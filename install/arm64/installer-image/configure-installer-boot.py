#!/usr/bin/env python3
"""Configure Pi 5 boot files inside an isolated staged target root.

This is a build step.  ``target_root`` is an extracted, package-verified
root tree; it is never inferred from the host and ``/`` is rejected.  The
normal mode only stages boot and mkinitcpio configuration.  ``--generate``
is a separate, explicit operation for a native aarch64 build host and runs
``systemd-nspawn -D TARGET_ROOT --register=no --private-network
/usr/bin/mkinitcpio -P`` after staging the files.  nspawn supplies private
/dev and /proc mounts and an isolated network namespace for the prepared root,
then tears them down when the command exits.

The script does not read the running machine's /boot, cmdline, EEPROM or
storage configuration.  In particular, it refuses LUKS-related kernel
arguments if a staged cmdline is already present.  The image assembler can
later insert the disposable target filesystem UUID into a generic cmdline.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import platform
import re
import shlex
import stat
import subprocess
import tempfile
from typing import Any, Callable, Sequence


class BootConfigurationError(RuntimeError):
    """A staged target cannot be configured without crossing a safety boundary."""


BOOT_DIRECTORY = Path("boot")
CONFIG_FILE = Path("boot/config.txt")
CMDLINE_FILE = Path("boot/cmdline.txt")
INITRAMFS_FILE = Path("boot/initramfs-linux.img")
MKINITCPIO_FRAGMENT = Path("etc/mkinitcpio.conf.d/90-omarchy-pi-installer.conf")
LINUX_RPI_PRESET_DIRECTORY = Path("etc/mkinitcpio.d")
MKINITCPIO_CONFIG = Path("etc/mkinitcpio.conf")

KERNEL_CANDIDATES = ("kernel8.img", "kernel_2712.img", "Image")
DTB_NAMES = (
    "bcm2712-rpi-5-b.dtb",
    "bcm2712-rpi-cm5-cm4io.dtb",
    "bcm2712-rpi-cm5-cm5io.dtb",
    "bcm2712-rpi-cm5l-cm4io.dtb",
    "bcm2712-rpi-cm5l-cm5io.dtb",
)
DTB_CANDIDATES = tuple(
    Path(directory) / name
    for directory in ("dtbs/broadcom", "broadcom", "")
    for name in DTB_NAMES
)
OVERLAY_FILE = Path("overlays/vc4-kms-v3d-pi5.dtbo")

MANAGED_START = "# BEGIN OMARCHY PI INSTALLER BOOT"
MANAGED_END = "# END OMARCHY PI INSTALLER BOOT"
MANAGED_CONFIG = (
    MANAGED_START,
    "[pi5]",
    "dtparam=pciex1_gen=1",
    "[all]",
    "kernel={kernel}",
    "initramfs initramfs-linux.img followkernel",
    "dtoverlay=vc4-kms-v3d-pi5",
    MANAGED_END,
)

MKINITCPIO_TEXT = """# Generated for the Omarchy Pi installer image.
# Keep storage support explicit so a USB root does not depend on device probing.
MODULES=(xhci_pci usb_storage uas usbhid hid_generic mmc_core mmc_block ext4)
HOOKS=(base systemd modconf keyboard sd-vconsole block filesystems fsck)
"""

_PCIE_LINE = re.compile(r"^\s*dtparam=pciex1_gen=", re.IGNORECASE)
_KERNEL_LINE = re.compile(r"^\s*kernel=", re.IGNORECASE)
_INITRAMFS_LINE = re.compile(r"^\s*initramfs\s+", re.IGNORECASE)
_OVERLAY_LINE = re.compile(r"^\s*dtoverlay=vc4-kms-v3d-pi5(?:\s|$)", re.IGNORECASE)
_LUKS_ARGUMENT = re.compile(
    r"^(?:rd\.luks(?:\.|=|$)|rd\.crypt(?:\.|=|$)|rd\.lvm(?:\.|=|$)|"
    r"cryptdevice(?:=|$)|cryptroot(?:=|$)|luks(?:=|\.|$))",
    re.IGNORECASE,
)
_SAFE_KERNEL_NAME = re.compile(r"(?:kernel8\.img|kernel_2712\.img|Image)\Z")
_ARM64_IMAGE_MAGIC_OFFSET = 0x38
_ARM64_IMAGE_MAGIC = b"ARM\x64"
_CONFIG_ASSIGNMENT = re.compile(r"^\s*(ALL_config|default_config)\s*=\s*(\S.*)$")
_REQUIRED_KERNEL_MODULES = {
    "usb_storage": ("usb-storage.ko", "usb_storage.ko"),
    "uas": ("uas.ko",),
    "xhci_pci": ("xhci-pci.ko", "xhci_pci.ko"),
    "ext4": ("ext4.ko",),
}
_PRESET_KERNEL_VERSION = re.compile(r"^\s*ALL_kver\s*=\s*(['\"])([^'\"]+)\1\s*$")


@dataclass(frozen=True, slots=True)
class BootConfigurationResult:
    """Non-sensitive facts about one staged configuration operation."""

    kernel: str
    dtb: str
    overlay: str
    initramfs: str
    generated: bool


Runner = Callable[..., Any]


def _absolute_lexical(path: Path) -> Path:
    if path.is_absolute():
        return Path(os.path.normpath(os.fspath(path)))
    return Path(os.path.normpath(os.path.join(os.getcwd(), os.fspath(path))))


def _reject_symlink_components(path: Path, *, include_leaf: bool = True) -> None:
    absolute = _absolute_lexical(path)
    current = Path(absolute.anchor)
    components = absolute.parts[1:]
    if not include_leaf:
        components = components[:-1]
    for component in components:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise BootConfigurationError(f"refusing symlink path component: {current}")


def validate_target_root(path: Path) -> Path:
    """Resolve a staged root lexically and reject the host root or symlinks."""

    candidate = _absolute_lexical(path)
    if candidate == Path("/"):
        raise BootConfigurationError("refusing the host root as the target root")
    _reject_symlink_components(candidate)
    try:
        info = candidate.lstat()
    except FileNotFoundError:
        raise BootConfigurationError("target root does not exist") from None
    if not stat.S_ISDIR(info.st_mode):
        raise BootConfigurationError("target root must be a directory")
    boot = candidate / BOOT_DIRECTORY
    _reject_symlink_components(boot)
    try:
        boot_info = boot.lstat()
    except FileNotFoundError:
        raise BootConfigurationError("target root has no boot directory") from None
    if not stat.S_ISDIR(boot_info.st_mode):
        raise BootConfigurationError("target boot must be a directory")
    return candidate


def _reject_fat_symlinks(boot: Path) -> None:
    """FAT cannot preserve links or device nodes; reject them before writes."""

    for directory, subdirectories, files in os.walk(boot, followlinks=False):
        for name in (*subdirectories, *files):
            candidate = Path(directory) / name
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise BootConfigurationError(f"boot contains a symlink unsupported by FAT: {candidate}")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise BootConfigurationError(f"boot contains an unsupported special file: {candidate}")


def _regular_file(path: Path, *, description: str, nonempty: bool = True) -> Path:
    _reject_symlink_components(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise BootConfigurationError(f"{description} is missing") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BootConfigurationError(f"{description} must be a regular file")
    if nonempty and info.st_size == 0:
        raise BootConfigurationError(f"{description} is empty")
    return path


def _optional_text(path: Path, *, description: str) -> str:
    if not os.path.lexists(path):
        return ""
    _regular_file(path, description=description, nonempty=False)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise BootConfigurationError(f"{description} is not readable UTF-8 text") from None


def _validate_no_luks_cmdline(path: Path) -> None:
    text = _optional_text(path, description="staged cmdline")
    for argument in text.split():
        if _LUKS_ARGUMENT.match(argument):
            raise BootConfigurationError("staged cmdline contains an unsupported LUKS argument")


def _select_kernel(boot: Path, requested: str | None) -> Path:
    candidates = (requested,) if requested is not None else KERNEL_CANDIDATES
    for name in candidates:
        if name is None or not _SAFE_KERNEL_NAME.fullmatch(name):
            raise BootConfigurationError("kernel name is not an allowed Pi boot filename")
        candidate = boot / name
        if os.path.lexists(candidate):
            selected = _regular_file(candidate, description=f"Pi kernel {name}")
            if not _is_arm64_linux_image(selected):
                raise BootConfigurationError(f"Pi kernel {name} is not an ARM64 Linux Image")
            return selected
    names = ", ".join(candidates)
    raise BootConfigurationError(f"no nonempty Pi kernel found (expected one of {names})")


def _is_arm64_linux_image(path: Path) -> bool:
    """Recognize the ARM64 Linux Image header and reject U-Boot payloads."""

    try:
        with path.open("rb") as stream:
            header = stream.read(_ARM64_IMAGE_MAGIC_OFFSET + len(_ARM64_IMAGE_MAGIC))
    except OSError:
        return False
    return len(header) >= _ARM64_IMAGE_MAGIC_OFFSET + len(_ARM64_IMAGE_MAGIC) and header[
        _ARM64_IMAGE_MAGIC_OFFSET : _ARM64_IMAGE_MAGIC_OFFSET + len(_ARM64_IMAGE_MAGIC)
    ] == _ARM64_IMAGE_MAGIC


def _select_dtb(boot: Path) -> Path:
    for relative in DTB_CANDIDATES:
        candidate = boot / relative
        if os.path.lexists(candidate):
            return _regular_file(candidate, description="Pi 5/CM5 device tree")
    expected = ", ".join(os.fspath(item) for item in DTB_CANDIDATES)
    raise BootConfigurationError(f"no nonempty Pi 5/CM5 device tree found (expected one of {expected})")


def _package_file_list(path: Path) -> set[str]:
    """Read the file list from one pacman local package record."""

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise BootConfigurationError("linux-rpi package file list is unreadable") from None
    in_files = False
    files: set[str] = set()
    for line in text.splitlines():
        if line == "%FILES%":
            in_files = True
            continue
        if in_files and line.startswith("%"):
            break
        if in_files and line:
            files.add(line.lstrip("/"))
    return files


def _validate_linux_rpi_provenance(root: Path, kernel: Path) -> None:
    """If pacman metadata exists, require the selected kernel to be linux-rpi-owned."""

    local = root / "var/lib/pacman/local"
    if not os.path.lexists(local):
        return
    _reject_symlink_components(local)
    if not local.is_dir():
        raise BootConfigurationError("target pacman local database is not a directory")
    records = sorted(entry for entry in local.iterdir() if entry.name.startswith("linux-rpi-"))
    if not records:
        raise BootConfigurationError("target pacman database has no linux-rpi package record")
    expected = f"boot/{kernel.name}"
    for record in records:
        _reject_symlink_components(record)
        if not record.is_dir():
            continue
        files_path = record / "files"
        if os.path.lexists(files_path):
            _regular_file(files_path, description="linux-rpi package file list", nonempty=False)
            if expected in _package_file_list(files_path):
                return
    raise BootConfigurationError(f"selected kernel {kernel.name} is not owned by a linux-rpi package")


def _validate_linux_rpi_preset(root: Path) -> None:
    """Ensure the linux-rpi preset leaves /etc/mkinitcpio.conf drop-ins active."""

    directory = root / LINUX_RPI_PRESET_DIRECTORY
    _reject_symlink_components(directory)
    if not directory.is_dir():
        raise BootConfigurationError("target has no mkinitcpio preset directory")
    presets = sorted(entry for entry in directory.iterdir() if entry.name.startswith("linux-rpi") and entry.name.endswith(".preset"))
    if not presets:
        raise BootConfigurationError("target has no linux-rpi mkinitcpio preset")
    for preset in presets:
        _regular_file(preset, description="linux-rpi mkinitcpio preset")
        try:
            text = preset.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise BootConfigurationError("linux-rpi mkinitcpio preset is unreadable") from None
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = _CONFIG_ASSIGNMENT.match(line)
            if match:
                try:
                    value = shlex.split(match.group(2), comments=False)[0]
                except (ValueError, IndexError):
                    raise BootConfigurationError("linux-rpi mkinitcpio preset has an invalid config assignment") from None
                if value != "/etc/mkinitcpio.conf":
                    raise BootConfigurationError("linux-rpi mkinitcpio preset bypasses /etc/mkinitcpio.conf drop-ins")


def _kernel_module_versions(root: Path) -> list[Path]:
    lib = root / "lib"
    if os.path.islink(lib):
        try:
            target = os.readlink(lib)
        except OSError as exc:
            raise BootConfigurationError("target /lib symlink is unreadable") from exc
        if target != "usr/lib":
            raise BootConfigurationError("target /lib symlink is not the reviewed merged-/usr link")
        # Arch Linux ARM uses the standard merged-/usr layout.  Validate the
        # link exactly, then continue checks from the real target so a random
        # symlink cannot redirect module inspection outside the rootfs.
        lib = root / "usr/lib"
    modules = lib / "modules"
    if not os.path.lexists(modules):
        return []
    _reject_symlink_components(modules)
    if not modules.is_dir():
        raise BootConfigurationError("target kernel modules path is not a directory")
    versions: list[Path] = []
    for entry in sorted(modules.iterdir()):
        if entry.is_dir() and not entry.is_symlink():
            builtin = entry / "modules.builtin"
            if os.path.lexists(builtin):
                _regular_file(builtin, description="target modules.builtin", nonempty=False)
                versions.append(entry)
    return versions


def _target_builtin_modules(root: Path) -> set[str]:
    """Return normalized module names built into the preset's target kernel."""

    versions = _kernel_module_versions(root)
    if not versions:
        return set()
    preset_version: str | None = None
    for preset in sorted((root / LINUX_RPI_PRESET_DIRECTORY).iterdir()):
        if not preset.name.startswith("linux-rpi") or not preset.name.endswith(".preset"):
            continue
        text = preset.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = _PRESET_KERNEL_VERSION.match(line)
            if match:
                if preset_version is not None and preset_version != match.group(2):
                    raise BootConfigurationError("linux-rpi presets select different kernel versions")
                preset_version = match.group(2)
    if preset_version is not None:
        versions = [entry for entry in versions if entry.name == preset_version]
        if not versions:
            raise BootConfigurationError("linux-rpi preset kernel has no modules.builtin")
    elif len(versions) != 1:
        raise BootConfigurationError("cannot identify the target kernel modules.builtin")
    try:
        builtin_text = (versions[0] / "modules.builtin").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise BootConfigurationError("target modules.builtin is unreadable") from None
    names: set[str] = set()
    for line in builtin_text.splitlines():
        basename = Path(line).name
        if basename.endswith(".ko"):
            names.add(basename[:-3].replace("-", "_"))
    return names


def _module_present(listing: str, aliases: tuple[str, ...], builtins: set[str], module: str) -> bool:
    if module in builtins:
        return True
    return any(alias in listing for alias in aliases)


def _read_boot_inputs(root: Path, kernel_name: str | None) -> tuple[Path, Path, Path, str]:
    boot = root / BOOT_DIRECTORY
    _reject_fat_symlinks(boot)
    kernel = _select_kernel(boot, kernel_name)
    _validate_linux_rpi_provenance(root, kernel)
    _validate_linux_rpi_preset(root)
    _regular_file(root / MKINITCPIO_CONFIG, description="target mkinitcpio.conf")
    initramfs = _regular_file(root / INITRAMFS_FILE, description="installer initramfs")
    dtb = _select_dtb(boot)
    overlay = _regular_file(boot / OVERLAY_FILE, description="Pi 5 DRM overlay")
    config = _optional_text(root / CONFIG_FILE, description="staged config.txt")
    if "\x00" in config:
        raise BootConfigurationError("staged config.txt contains a NUL byte")
    _validate_no_luks_cmdline(root / CMDLINE_FILE)
    return kernel, dtb, overlay, config


def _without_managed_block(lines: list[str]) -> list[str]:
    result: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == MANAGED_START:
            inside = True
            continue
        if line.strip() == MANAGED_END:
            inside = False
            continue
        if inside:
            continue
        if _KERNEL_LINE.match(line) or _INITRAMFS_LINE.match(line) or _OVERLAY_LINE.match(line) or _PCIE_LINE.match(line):
            continue
        result.append(line)
    if inside:
        raise BootConfigurationError("staged config.txt has an incomplete managed block")
    while result and not result[-1].strip():
        result.pop()
    return result


def build_config(text: str, *, kernel: str) -> str:
    """Preserve unrelated firmware settings and append one managed Pi block."""

    lines = _without_managed_block(text.splitlines())
    if lines:
        lines.append("")
    lines.extend(item.format(kernel=kernel) for item in MANAGED_CONFIG)
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, data: bytes, *, mode: int) -> None:
    _reject_symlink_components(path, include_leaf=False)
    if os.path.lexists(path):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise BootConfigurationError(f"refusing to replace symlink: {path}")
        if not stat.S_ISREG(info.st_mode):
            raise BootConfigurationError(f"refusing to replace non-file: {path}")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise BootConfigurationError(f"parent directory is unsafe: {parent}")
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".omarchy-pi.", dir=parent)
        temporary = Path(temporary_name)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        os.chmod(path, mode)
    except OSError:
        raise BootConfigurationError(f"could not write staged file: {path}") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _run_native_mkinitcpio(root: Path, *, runner: Runner, machine: str) -> None:
    if machine not in {"aarch64", "arm64"}:
        raise BootConfigurationError("--generate requires a native aarch64 build host")
    _regular_file(root / "usr/bin/mkinitcpio", description="target mkinitcpio")
    command = [
        "systemd-nspawn",
        "-D",
        os.fspath(root),
        "--register=no",
        "--private-network",
        "/usr/bin/mkinitcpio",
        "-P",
    ]
    try:
        result = runner(command, check=False)
    except (OSError, subprocess.SubprocessError):
        raise BootConfigurationError("native mkinitcpio invocation failed to start") from None
    if getattr(result, "returncode", 1) != 0:
        raise BootConfigurationError("native mkinitcpio generation failed")
    _regular_file(root / "usr/bin/lsinitcpio", description="target lsinitcpio")
    inspect_command = [
        "systemd-nspawn",
        "-D",
        os.fspath(root),
        "--register=no",
        "--private-network",
        "/usr/bin/lsinitcpio",
        "-l",
        "/boot/initramfs-linux.img",
    ]
    try:
        inspected = runner(inspect_command, check=False, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        raise BootConfigurationError("could not inspect generated initramfs") from None
    if getattr(inspected, "returncode", 1) != 0:
        raise BootConfigurationError("generated initramfs inspection failed")
    listing = getattr(inspected, "stdout", "") or ""
    builtins = _target_builtin_modules(root)
    missing = [
        module
        for module, aliases in _REQUIRED_KERNEL_MODULES.items()
        if not _module_present(listing, aliases, builtins, module)
    ]
    if missing:
        raise BootConfigurationError("generated initramfs is missing required USB or ext4 support")


def configure_installer_boot(
    target_root: Path,
    *,
    kernel_name: str | None = None,
    generate: bool = False,
    runner: Runner = subprocess.run,
    machine: str | None = None,
) -> BootConfigurationResult:
    """Stage boot policy in ``target_root`` and optionally generate initramfs."""

    root = validate_target_root(target_root)
    kernel, dtb, overlay, config = _read_boot_inputs(root, kernel_name)
    fragment = root / MKINITCPIO_FRAGMENT
    _reject_symlink_components(fragment, include_leaf=False)
    if os.path.lexists(fragment):
        _regular_file(fragment, description="existing mkinitcpio installer fragment", nonempty=False)
    if "autodetect" in MKINITCPIO_TEXT.lower():
        raise BootConfigurationError("installer mkinitcpio fragment must not use autodetect")

    _atomic_write(root / CONFIG_FILE, build_config(config, kernel=kernel.name).encode("utf-8"), mode=0o644)
    fragment.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(fragment, MKINITCPIO_TEXT.encode("utf-8"), mode=0o644)
    _reject_fat_symlinks(root / BOOT_DIRECTORY)

    if generate:
        _run_native_mkinitcpio(root, runner=runner, machine=machine or platform.machine())
        _regular_file(root / INITRAMFS_FILE, description="generated installer initramfs")
        _reject_fat_symlinks(root / BOOT_DIRECTORY)
    return BootConfigurationResult(
        kernel=str(kernel.relative_to(root / BOOT_DIRECTORY)),
        dtb=str(dtb.relative_to(root / BOOT_DIRECTORY)),
        overlay=str(overlay.relative_to(root / BOOT_DIRECTORY)),
        initramfs=str(INITRAMFS_FILE.relative_to(BOOT_DIRECTORY)),
        generated=generate,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage Pi 5 installer boot policy in an isolated target root"
    )
    parser.add_argument("target_root", type=Path)
    parser.add_argument(
        "--kernel",
        dest="kernel_name",
        choices=KERNEL_CANDIDATES,
        help="explicit kernel filename; otherwise choose kernel8.img then kernel_2712.img then Image",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="after staging, run mkinitcpio in an isolated systemd-nspawn root on native aarch64",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if os.geteuid() != 0:
        print("configure-installer-boot: staged configuration must run as root to preserve target ownership", file=os.sys.stderr)
        return 2
    try:
        result = configure_installer_boot(
            args.target_root,
            kernel_name=args.kernel_name,
            generate=args.generate,
        )
    except BootConfigurationError as exc:
        print(f"configure-installer-boot: error: {exc}", file=os.sys.stderr)
        return 1
    print(
        "configured staged Pi boot: "
        f"kernel={result.kernel} dtb={result.dtb} overlay={result.overlay} "
        f"initramfs={result.initramfs} generated={result.generated}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
