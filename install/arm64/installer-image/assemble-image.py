#!/usr/bin/env python3
"""Assemble a Raspberry Pi image from an already prepared root tree.

This is deliberately an offline image *assembler*.  It does not resolve or
download packages, contact a Pi, or write a block device.  The caller supplies
a root tree containing the target-side files, including the Pi boot files in
``boot/``.

The final filesystem creation and mounting commands need the normal Linux
privileges for ``sfdisk``, ``losetup``, ``mkfs.*`` and ``mount``.  Unit tests
exercise the planning and rewriting paths without requiring those privileges.
Root contents are streamed through a local GNU-tar-compatible ``tar`` with
ACL, xattr, hardlink, owner and symlink preservation enabled.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import BinaryIO, Sequence


SECTOR_SIZE = 512
MIB = 1024 * 1024
FIRST_PARTITION_SECTOR = 2048
DEFAULT_BOOT_SIZE_MIB = 512
# Leave room for ext4 metadata, allocation rounding, and files whose on-disk
# footprint exceeds their apparent size when copying a prepared package root.
DEFAULT_ROOT_EXTRA_MIB = 1024
MIN_ROOT_SIZE_MIB = 1024
LOOP_DEVICE_PATTERN = re.compile(r"^/dev/loop[0-9]+$")
UUID_PATTERN = re.compile(r"^[0-9A-Fa-f-]+$")
ELF_MACHINE = {
    "aarch64": 183,
    "arm64": 183,
    "x86_64": 62,
    "amd64": 62,
    "armv7l": 40,
    "arm": 40,
}


class ImageAssemblyError(RuntimeError):
    """A requested image cannot be assembled safely."""


@dataclass(frozen=True)
class ImageLayout:
    boot_size_mib: int
    root_size_mib: int
    boot_start_sector: int
    boot_sectors: int
    root_start_sector: int
    root_sectors: int

    @property
    def image_bytes(self) -> int:
        return (self.root_start_sector + self.root_sectors) * SECTOR_SIZE


def _positive_int(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _resolve_existing_directory(path: Path, *, name: str) -> Path:
    if os.path.lexists(path) and path.is_symlink():
        raise ImageAssemblyError(f"{name} must not be a symlink: {path}")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ImageAssemblyError(f"{name} does not exist: {path}") from exc
    if not resolved.is_dir():
        raise ImageAssemblyError(f"{name} is not a directory: {path}")
    return resolved


def _is_nonempty_regular(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size > 0


def _reject_symlink_components(path: Path, *, include_leaf: bool = True) -> None:
    # Keep ``..`` while walking: normalizing first could hide a symlink in
    # ``link/../image`` even though the kernel traverses ``link``.
    candidate = path if path.is_absolute() else Path.cwd() / path
    components = candidate.parts[1:]
    if not include_leaf:
        components = components[:-1]
    current = Path(candidate.anchor)
    for component in components:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise ImageAssemblyError(f"refusing symlink path component: {current}")


def validate_mkfs_fat_executable(executable: Path | str = "mkfs.fat") -> str:
    """Resolve and validate the native dosfstools executable used for FAT."""

    requested = os.fspath(executable)
    resolved = shutil.which(requested) if "/" not in requested else requested
    if not resolved:
        raise ImageAssemblyError(f"mkfs.fat executable was not found: {requested}")
    path = Path(resolved)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ImageAssemblyError(f"cannot inspect mkfs.fat executable: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise ImageAssemblyError(f"mkfs.fat executable must be a regular executable file: {path}")
    try:
        with path.open("rb") as stream:
            header = stream.read(20)
    except OSError as exc:
        raise ImageAssemblyError(f"cannot read mkfs.fat executable: {path}") from exc
    if len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != 2:
        raise ImageAssemblyError(f"mkfs.fat executable must be a 64-bit ELF binary: {path}")
    if header[5] == 1:
        machine = struct.unpack_from("<H", header, 18)[0]
    elif header[5] == 2:
        machine = struct.unpack_from(">H", header, 18)[0]
    else:
        raise ImageAssemblyError(f"mkfs.fat executable has an invalid ELF byte order: {path}")
    expected_machine = ELF_MACHINE.get(platform.machine().lower())
    if expected_machine is None or machine != expected_machine:
        raise ImageAssemblyError(
            f"mkfs.fat executable is not native to this build host: {path}"
        )
    try:
        result = subprocess.run(
            [os.fspath(path), "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ImageAssemblyError(f"mkfs.fat --help failed to run: {path}") from exc
    if result.returncode != 0:
        raise ImageAssemblyError(f"mkfs.fat --help failed: {path}")
    return os.fspath(path)


def _validate_tree_entries(staging: Path) -> None:
    """Reject device nodes and other entries the copy routine cannot safely clone."""

    for root, dirs, files in os.walk(staging, followlinks=False):
        for name in (*dirs, *files):
            candidate = Path(root) / name
            mode = candidate.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                raise ImageAssemblyError(f"staging contains unsupported special file: {candidate}")


def validate_staging_directory(staging: Path) -> Path:
    """Validate and return a safe, nonempty pre-staged root tree."""

    resolved = _resolve_existing_directory(staging, name="staging directory")
    if resolved == Path("/"):
        raise ImageAssemblyError("refusing the host root as the staging directory")
    entries = list(resolved.iterdir())
    if not entries:
        raise ImageAssemblyError("staging directory is empty")
    if not any(entry.name != "boot" for entry in entries):
        raise ImageAssemblyError("staging directory has no root files outside boot/")
    _validate_tree_entries(resolved)

    boot = resolved / "boot"
    if boot.is_symlink() or not boot.is_dir():
        raise ImageAssemblyError("staging directory must contain a real boot/ directory")
    validate_boot_inputs(boot)
    validate_boot_tree_for_fat(boot)
    return resolved


def validate_boot_inputs(boot: Path) -> None:
    """Require nonempty Pi kernel and initramfs inputs in ``boot``."""

    for name in ("config.txt", "cmdline.txt"):
        candidate = boot / name
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
            raise ImageAssemblyError(f"staging boot/{name} must be a regular file when present")

    kernel_names = {
        "Image",
        "kernel8.img",
        "kernel_2712.img",
        "kernel7l.img",
        "kernel7.img",
    }
    kernels = []
    initramfs = []
    for entry in boot.iterdir():
        if entry.is_symlink() or not entry.is_file():
            continue
        if entry.name in kernel_names or entry.name.startswith("vmlinuz") or (entry.name.startswith("kernel") and entry.name.endswith(".img")):
            kernels.append(entry)
        if entry.name.startswith("initramfs"):
            initramfs.append(entry)
    if not any(_is_nonempty_regular(entry) for entry in kernels):
        raise ImageAssemblyError(
            "staging boot/ has no nonempty Pi kernel (expected kernel8.img, kernel_2712.img, Image, or kernel*.img)"
        )
    if not any(_is_nonempty_regular(entry) for entry in initramfs):
        raise ImageAssemblyError("staging boot/ has no nonempty initramfs file")


def validate_boot_tree_for_fat(boot: Path) -> None:
    """Reject boot entries FAT cannot represent, especially symlinks."""

    for root, dirs, files in os.walk(boot, followlinks=False):
        for name in (*dirs, *files):
            candidate = Path(root) / name
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ImageAssemblyError(f"staging boot/ contains a symlink unsupported by FAT: {candidate}")
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ImageAssemblyError(f"staging boot/ contains unsupported special file: {candidate}")


def validate_output_path(output: Path) -> Path:
    """Validate an output path before any file is created."""

    _reject_symlink_components(output, include_leaf=False)
    if os.path.lexists(output):
        raise ImageAssemblyError(f"refusing to overwrite existing output: {output}")
    try:
        parent = output.parent.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ImageAssemblyError(f"output parent does not exist: {output.parent}") from exc
    if not parent.is_dir():
        raise ImageAssemblyError(f"output parent is not a directory: {parent}")

    # A missing path under these trees could otherwise be created as a regular
    # file while looking like a harmless image path.  Existing device nodes are
    # already rejected by the no-overwrite check above.
    for device_tree in (Path("/dev"), Path("/proc"), Path("/sys"), Path("/run")):
        try:
            parent.relative_to(device_tree)
        except ValueError:
            continue
        raise ImageAssemblyError(f"refusing an image output under {device_tree}: {output}")
    return output


def validate_output_outside_staging(output: Path, staging: Path) -> None:
    """Prevent the output image from becoming part of its own source tree."""

    output_parent = output.parent.resolve(strict=True)
    try:
        output_parent.relative_to(staging)
    except ValueError:
        return
    raise ImageAssemblyError(
        f"refusing an output inside the staging tree: {output} (staging: {staging})"
    )


def estimate_root_payload_bytes(staging: Path) -> int:
    """Estimate regular-file bytes copied to ext4, excluding the FAT boot tree."""

    total = 0
    for root, dirs, files in os.walk(staging, followlinks=False):
        relative = Path(root).relative_to(staging)
        if relative == Path("boot") or Path("boot") in relative.parents:
            dirs[:] = []
            continue
        for filename in files:
            candidate = Path(root) / filename
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def estimate_ext4_payload_bytes(staging: Path) -> int:
    """Budget 4 KiB allocation and inodes, including directories/symlinks."""
    total = 4096
    for current, dirs, files in os.walk(staging, followlinks=False):
        relative = Path(current).relative_to(staging)
        if relative == Path("boot") or Path("boot") in relative.parents:
            dirs[:] = []
            continue
        if relative == Path("."):
            dirs[:] = [name for name in dirs if name != "boot"]
        for name in (*dirs, *files):
            info = (Path(current) / name).lstat()
            total += 512  # inode and extended metadata allowance
            if stat.S_ISREG(info.st_mode):
                total += ((info.st_size + 4095) // 4096) * 4096
            elif stat.S_ISDIR(info.st_mode):
                total += 4096
            elif stat.S_ISLNK(info.st_mode):
                total += 4096  # conservative even for inline short links
    return total


def recommended_root_size_mib(staging: Path, extra_mib: int = DEFAULT_ROOT_EXTRA_MIB) -> int:
    payload_mib = (estimate_ext4_payload_bytes(staging) + MIB - 1) // MIB
    # Keep the requested allowance usable after ext4's default 5% reserve,
    # inode tables, journal, allocation rounding and filesystem metadata.
    usable_mib = payload_mib + extra_mib + 128
    return max(MIN_ROOT_SIZE_MIB, (usable_mib * 100 + 89) // 90)


def make_layout(boot_size_mib: int, root_size_mib: int) -> ImageLayout:
    if boot_size_mib <= 0 or root_size_mib <= 0:
        raise ImageAssemblyError("partition sizes must be positive")
    boot_sectors = boot_size_mib * MIB // SECTOR_SIZE
    root_sectors = root_size_mib * MIB // SECTOR_SIZE
    root_start_sector = FIRST_PARTITION_SECTOR + boot_sectors
    return ImageLayout(
        boot_size_mib=boot_size_mib,
        root_size_mib=root_size_mib,
        boot_start_sector=FIRST_PARTITION_SECTOR,
        boot_sectors=boot_sectors,
        root_start_sector=root_start_sector,
        root_sectors=root_sectors,
    )


def partition_script(layout: ImageLayout) -> str:
    """Return an MBR/DOS partition table description for sfdisk."""

    return (
        "label: dos\n"
        "unit: sectors\n"
        "\n"
        f"start={layout.boot_start_sector}, size={layout.boot_sectors}, type=c, bootable\n"
        f"start={layout.root_start_sector}, size={layout.root_sectors}, type=83\n"
    )


def rewrite_fstab(text: str, *, root_uuid: str, boot_uuid: str) -> str:
    """Keep unrelated fstab lines and point / and /boot at fresh UUIDs."""

    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            fields = stripped.split()
            if len(fields) >= 2 and fields[1] in {"/", "/boot"}:
                continue
        kept.append(line)
    while kept and kept[-1] == "":
        kept.pop()
    kept.extend(
        [
            f"UUID={root_uuid} / ext4 defaults 0 1",
            f"UUID={boot_uuid} /boot vfat defaults 0 2",
        ]
    )
    return "\n".join(kept) + "\n"


def rewrite_cmdline(text: str, *, root_uuid: str) -> str:
    """Replace any root selector and ensure ext4/rootwait boot arguments."""

    tokens = text.split()
    result: list[str] = []
    root_seen = False
    fstype_seen = False
    wait_seen = False
    for token in tokens:
        if token.startswith("root="):
            if not root_seen:
                result.append(f"root=UUID={root_uuid}")
                root_seen = True
            continue
        if token.startswith("rootfstype="):
            if not fstype_seen:
                result.append("rootfstype=ext4")
                fstype_seen = True
            continue
        if token == "rootwait":
            if not wait_seen:
                result.append(token)
                wait_seen = True
            continue
        result.append(token)
    if not root_seen:
        result.append(f"root=UUID={root_uuid}")
    if not fstype_seen:
        result.append("rootfstype=ext4")
    if not wait_seen:
        result.append("rootwait")
    return " ".join(result) + "\n"


_PCIE_LINE = re.compile(r"^(?P<prefix>\s*dtparam=pciex1_gen=)\d+(?P<suffix>\s*(?:#.*)?)$")


def ensure_gen1_config(text: str) -> str:
    """Ensure the image's new-base Pi policy selects external PCIe Gen1."""

    lines = text.splitlines()
    found = False
    for index, line in enumerate(lines):
        match = _PCIE_LINE.match(line)
        if match:
            lines[index] = f"{match.group('prefix')}1{match.group('suffix')}"
            found = True
    if not found:
        if lines:
            lines.append("")
        lines.extend(["[pi5]", "dtparam=pciex1_gen=1"])
    return "\n".join(lines).rstrip() + "\n"


def _copy_boot_entry(source: Path, destination: Path) -> None:
    """Copy one validated regular boot entry to the FAT mount."""

    info = source.lstat()
    if stat.S_ISDIR(info.st_mode):
        destination.mkdir(parents=True, exist_ok=False)
        for child in source.iterdir():
            _copy_boot_entry(child, destination / child.name)
    elif stat.S_ISREG(info.st_mode):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination, follow_symlinks=False)
    else:
        raise ImageAssemblyError(f"staging boot/ contains unsupported special file: {source}")


def _run_tar_copy(staging: Path, root_mount: Path) -> None:
    """Archive root entries with tar metadata flags and extract them."""

    entries = [entry.name for entry in staging.iterdir() if entry.name != "boot"]
    if not entries:
        raise ImageAssemblyError("staging directory has no root files outside boot/")
    archive_command = [
        "tar",
        "--create",
        "--file=-",
        "--directory",
        str(staging),
        "--numeric-owner",
        "--acls",
        "--xattrs",
        "--xattrs-include=*",
        "--format=pax",
        "--",
        *entries,
    ]
    extract_command = [
        "tar",
        "--extract",
        "--file=-",
        "--directory",
        str(root_mount),
        "--numeric-owner",
        "--same-owner",
        "--same-permissions",
        "--acls",
        "--xattrs",
        "--xattrs-include=*",
    ]
    def terminate_archive(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def read_archive_stderr(stream: BinaryIO) -> str:
        max_bytes = 64 * 1024
        stream.flush()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - max_bytes))
        detail = stream.read(max_bytes).decode(errors="replace")
        if size > max_bytes:
            detail = "[earlier tar diagnostics truncated]\n" + detail
        return detail

    extracted = None
    extract_error: BaseException | None = None
    archive_returncode: int | None = None
    archive_stderr = ""
    with tempfile.TemporaryFile() as archive_stderr_file:
        try:
            archive = subprocess.Popen(
                archive_command,
                stdout=subprocess.PIPE,
                stderr=archive_stderr_file,
            )
        except OSError as exc:
            raise ImageAssemblyError(f"could not start metadata-preserving tar archive: {exc}") from exc
        assert archive.stdout is not None
        try:
            try:
                extracted = subprocess.run(
                    extract_command,
                    stdin=archive.stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
            except BaseException as exc:
                extract_error = exc
        finally:
            archive.stdout.close()
            if extract_error is not None or (extracted is not None and extracted.returncode != 0):
                terminate_archive(archive)
            archive_returncode = archive.wait()
        archive_stderr = read_archive_stderr(archive_stderr_file)
    if extract_error is not None:
        raise extract_error
    assert extracted is not None
    extract_stderr = extracted.stderr.decode(errors="replace")
    if extracted.returncode != 0:
        detail = extract_stderr.strip() or "no diagnostic"
        raise ImageAssemblyError(f"tar metadata-preserving extraction failed: {detail}")
    if archive_returncode != 0:
        detail = archive_stderr.strip() or "no diagnostic"
        raise ImageAssemblyError(f"tar metadata-preserving archive failed: {detail}")


def copy_root_without_boot(staging: Path, root_mount: Path) -> None:
    """Copy the root tree while reserving /boot for the FAT partition."""

    _run_tar_copy(staging, root_mount)


def copy_boot_tree(staging_boot: Path, boot_mount: Path) -> None:
    for entry in staging_boot.iterdir():
        _copy_boot_entry(entry, boot_mount / entry.name)


def _unlink_if_present(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        raise ImageAssemblyError(f"refusing to remove a directory while sanitizing image: {path}")
    path.unlink()


def sanitize_identity_files(root_mount: Path) -> None:
    """Remove machine identity and SSH host keys from the image copy."""

    etc = root_mount / "etc"
    if os.path.lexists(etc) and (etc.is_symlink() or not etc.is_dir()):
        raise ImageAssemblyError("image /etc must be a real directory")
    _unlink_if_present(etc / "machine-id")
    _unlink_if_present(root_mount / "var/lib/dbus/machine-id")
    ssh_directory = etc / "ssh"
    if os.path.lexists(ssh_directory) and (ssh_directory.is_symlink() or not ssh_directory.is_dir()):
        raise ImageAssemblyError("image /etc/ssh must be a real directory")
    if os.path.lexists(ssh_directory):
        for entry in ssh_directory.iterdir():
            if entry.name.startswith("ssh_host_"):
                _unlink_if_present(entry)


def mask_interactive_firstboot(root_mount: Path) -> None:
    """Mask systemd's console-based first-boot wizard in the image copy.

    The installer deliberately starts with no machine-id so the access
    provisioner can create a unique one from the installer settings.  The
    stock ``systemd-firstboot.service`` interprets that state as permission
    to prompt on the console, which is unavailable for a headless Pi boot
    and blocks ``sysinit.target`` before the installer services can run.
    """

    etc = root_mount / "etc"
    if os.path.lexists(etc) and (etc.is_symlink() or not etc.is_dir()):
        raise ImageAssemblyError("image /etc must be a real directory")
    etc.mkdir(parents=True, exist_ok=True)

    systemd = etc / "systemd"
    if os.path.lexists(systemd) and (systemd.is_symlink() or not systemd.is_dir()):
        raise ImageAssemblyError("image /etc/systemd must be a real directory")
    systemd.mkdir(exist_ok=True)

    system_units = systemd / "system"
    if os.path.lexists(system_units) and (system_units.is_symlink() or not system_units.is_dir()):
        raise ImageAssemblyError("image /etc/systemd/system must be a real directory")
    system_units.mkdir(exist_ok=True)

    mask = system_units / "systemd-firstboot.service"
    _unlink_if_present(mask)
    mask.symlink_to("/dev/null")


def _read_regular_or_empty(path: Path) -> str:
    if path.is_symlink():
        raise ImageAssemblyError(f"refusing to rewrite symlink: {path}")
    if not path.exists():
        return ""
    if not path.is_file():
        raise ImageAssemblyError(f"expected a regular file: {path}")
    return path.read_text(encoding="utf-8")


def adapt_copied_configuration(root_mount: Path, boot_mount: Path, *, root_uuid: str, boot_uuid: str) -> None:
    etc = root_mount / "etc"
    if os.path.lexists(etc) and (etc.is_symlink() or not etc.is_dir()):
        raise ImageAssemblyError("image /etc must be a real directory")
    etc.mkdir(parents=True, exist_ok=True)
    fstab = etc / "fstab"
    fstab.write_text(rewrite_fstab(_read_regular_or_empty(fstab), root_uuid=root_uuid, boot_uuid=boot_uuid), encoding="utf-8")

    cmdline = boot_mount / "cmdline.txt"
    cmdline.write_text(rewrite_cmdline(_read_regular_or_empty(cmdline), root_uuid=root_uuid), encoding="utf-8")
    config = boot_mount / "config.txt"
    config.write_text(ensure_gen1_config(_read_regular_or_empty(config)), encoding="utf-8")
    if not _is_nonempty_regular(config):
        raise ImageAssemblyError("assembled boot/config.txt is empty")


def _run(command: Sequence[str], *, input_text: str | None = None, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {"check": True}
    if input_text is not None:
        kwargs["input"] = input_text
        kwargs["text"] = True
    if capture_output:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    return subprocess.run(list(command), **kwargs)  # type: ignore[arg-type]


def _read_uuid(device: str) -> str:
    result = _run(["blkid", "-s", "UUID", "-o", "value", device], capture_output=True)
    value = result.stdout.strip()
    if not value or not UUID_PATTERN.fullmatch(value):
        raise ImageAssemblyError(f"could not read a filesystem UUID from {device}")
    return value


def _partition_device(loop_device: str, number: int) -> str:
    if not LOOP_DEVICE_PATTERN.fullmatch(loop_device):
        raise ImageAssemblyError(f"losetup returned a non-loop device: {loop_device!r}")
    return f"{loop_device}p{number}"


def _wait_for_partition_device(device: str, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            info = os.stat(device)
        except FileNotFoundError:
            time.sleep(0.1)
            continue
        if not stat.S_ISBLK(info.st_mode):
            raise ImageAssemblyError(f"partition path is not a block device: {device}")
        return
    raise ImageAssemblyError(f"partition device did not appear: {device}")


def _create_output(output: Path, size: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(output, flags, 0o600)
    created_info = os.fstat(descriptor)
    try:
        os.ftruncate(descriptor, size)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ImageAssemblyError(f"created output is not a regular file: {output}")
    except BaseException:
        # If sizing fails (for example, because the destination filesystem is
        # full), remove only the inode opened by this invocation.  A race that
        # replaced the path is left untouched.
        try:
            current = output.lstat()
            if current.st_dev == created_info.st_dev and current.st_ino == created_info.st_ino:
                output.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)


def _cleanup_command(command: Sequence[str]) -> bool:
    try:
        _run(command)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def assemble_image(
    staging: Path,
    output: Path,
    *,
    boot_size_mib: int = DEFAULT_BOOT_SIZE_MIB,
    root_size_mib: int | None = None,
    root_extra_mib: int = DEFAULT_ROOT_EXTRA_MIB,
    mkfs_fat: Path | str = "mkfs.fat",
) -> dict[str, object]:
    """Build one regular image file and return its layout and UUID metadata."""

    stage = validate_staging_directory(staging)
    validate_output_path(output)
    validate_output_outside_staging(output, stage)
    mkfs_fat_command = validate_mkfs_fat_executable(mkfs_fat)
    if root_extra_mib < 0:
        raise ImageAssemblyError("root extra space must not be negative")
    selected_root_size = root_size_mib or recommended_root_size_mib(stage, root_extra_mib)
    if selected_root_size < MIN_ROOT_SIZE_MIB:
        raise ImageAssemblyError(f"root size must be at least {MIN_ROOT_SIZE_MIB} MiB")
    layout = make_layout(boot_size_mib, selected_root_size)
    payload = estimate_root_payload_bytes(stage)
    if layout.root_sectors * SECTOR_SIZE < payload + 128 * MIB:
        raise ImageAssemblyError("root partition is too small for the staged tree and filesystem overhead")

    _create_output(output, layout.image_bytes)
    loop_device: str | None = None
    root_mounted = False
    boot_mounted = False
    completed = False
    mount_dir: Path | None = None
    boot_uuid: str | None = None
    root_uuid: str | None = None
    cleanup_failures: list[str] = []
    try:
        _run(["sfdisk", "--no-reread", str(output)], input_text=partition_script(layout))
        loop_result = _run(["losetup", "--find", "--show", "--partscan", str(output)], capture_output=True)
        loop_device = loop_result.stdout.strip()
        if not LOOP_DEVICE_PATTERN.fullmatch(loop_device):
            raise ImageAssemblyError(f"losetup returned an unsafe device path: {loop_device!r}")
        boot_device = _partition_device(loop_device, 1)
        root_device = _partition_device(loop_device, 2)
        _wait_for_partition_device(boot_device)
        _wait_for_partition_device(root_device)
        root_uuid_requested = str(uuid.uuid4())
        boot_serial = uuid.uuid4().hex[:8].upper()
        _run([mkfs_fat_command, "-F", "32", "-n", "PI-BOOT", "-i", boot_serial, boot_device])
        _run(["mkfs.ext4", "-F", "-U", root_uuid_requested, "-L", "OMARCHY-ROOT", root_device])
        boot_uuid = _read_uuid(boot_device)
        root_uuid = _read_uuid(root_device)
        if boot_uuid == root_uuid:
            raise ImageAssemblyError("boot and root filesystem UUIDs unexpectedly match")

        mount_dir = Path(tempfile.mkdtemp(prefix="omarchy-image-"))
        mount_root = mount_dir / "root"
        mount_root.mkdir()
        _run(["mount", "--", root_device, str(mount_root)])
        root_mounted = True
        boot_mount = mount_root / "boot"
        boot_mount.mkdir()
        _run(["mount", "-t", "vfat", "--", boot_device, str(boot_mount)])
        boot_mounted = True
        copy_root_without_boot(stage, mount_root)
        copy_boot_tree(stage / "boot", boot_mount)
        adapt_copied_configuration(mount_root, boot_mount, root_uuid=root_uuid, boot_uuid=boot_uuid)
        sanitize_identity_files(mount_root)
        mask_interactive_firstboot(mount_root)
        _run(["sync"])
        completed = True
    finally:
        if boot_mounted:
            if _cleanup_command(["umount", "--", str(mount_dir / "root" / "boot")]):
                boot_mounted = False
            else:
                cleanup_failures.append("boot unmount")
        if root_mounted:
            if _cleanup_command(["umount", "--", str(mount_dir / "root")]):
                root_mounted = False
            else:
                cleanup_failures.append("root unmount")
        if mount_dir is not None and not boot_mounted and not root_mounted:
            shutil.rmtree(mount_dir, ignore_errors=True)
        if loop_device is not None:
            if _cleanup_command(["losetup", "--detach", loop_device]):
                loop_device = None
            else:
                cleanup_failures.append("loop detach")
        if not completed and not cleanup_failures:
            try:
                output.unlink()
            except FileNotFoundError:
                pass
        if cleanup_failures:
            raise ImageAssemblyError(
                "cleanup failed (" + ", ".join(cleanup_failures) + "); output retained for recovery"
            )
    os.chmod(output, 0o644)
    return {
        "output": str(output),
        "mkfs_fat": mkfs_fat_command,
        "boot_uuid": boot_uuid,
        "root_uuid": root_uuid,
        "layout": layout,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", type=Path, required=True, help="pre-staged aarch64 root tree")
    parser.add_argument("--output", type=Path, required=True, help="new regular image file to create")
    parser.add_argument("--boot-size-mib", type=_positive_int, default=DEFAULT_BOOT_SIZE_MIB)
    parser.add_argument("--root-size-mib", type=_positive_int, default=None)
    parser.add_argument("--root-extra-mib", type=_positive_int, default=DEFAULT_ROOT_EXTRA_MIB)
    parser.add_argument(
        "--mkfs-fat",
        default="mkfs.fat",
        help="native dosfstools executable; defaults to mkfs.fat on the build host",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = assemble_image(
            args.staging,
            args.output,
            boot_size_mib=args.boot_size_mib,
            root_size_mib=args.root_size_mib,
            root_extra_mib=args.root_extra_mib,
            mkfs_fat=args.mkfs_fat,
        )
    except (ImageAssemblyError, OSError, subprocess.CalledProcessError) as exc:
        print(f"assemble-image: error: {exc}", file=os.sys.stderr)
        return 2
    layout = result["layout"]
    assert isinstance(layout, ImageLayout)
    print(
        f"assembled {result['output']} ({layout.image_bytes} bytes); "
        f"boot UUID {result['boot_uuid']}; root UUID {result['root_uuid']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
