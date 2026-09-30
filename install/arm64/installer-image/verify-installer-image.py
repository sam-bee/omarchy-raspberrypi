#!/usr/bin/env python3
"""Verify one completed Raspberry Pi installer image without modifying it.

The input must be a new regular image file.  Verification attaches it through
a read-only loop device, mounts both partitions read-only, checks the DOS
layout, boot contract, installer services, and identity sanitization, then
removes every temporary mount and loop attachment.  A block device, symlink,
or live filesystem is never accepted.
"""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable, Sequence


sys.path.insert(0, str(Path(__file__).resolve().parent))
import desktop_payload
import installer_payload_manifest as payload_manifest

FIRST_PARTITION_SECTOR = 2048
BOOT_TYPE = "c"
ROOT_TYPE = "83"
LOOP_DEVICE_PATTERN = re.compile(r"^/dev/loop[0-9]+$")
UUID_PATTERN = re.compile(r"^(?=[0-9A-Fa-f-]{8,}$)[0-9A-Fa-f]+(?:-[0-9A-Fa-f]+)*$")
KERNEL_NAMES = frozenset({"kernel8.img", "kernel_2712.img", "Image"})
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
ARM64_IMAGE_MAGIC_OFFSET = 0x38
ARM64_IMAGE_MAGIC = b"ARM\x64"
LUKS_ARGUMENT = re.compile(
    r"^(?:rd\.luks(?:\.|=|$)|rd\.crypt(?:\.|=|$)|rd\.lvm(?:\.|=|$)|"
    r"cryptdevice(?:=|$)|cryptroot(?:=|$)|luks(?:=|\.|$))",
    re.IGNORECASE,
)

BUILDER_MARKER = Path(payload_manifest.BUILDER_MARKER)
BUILDER_MARKER_CONTENT = payload_manifest.BUILDER_MARKER_CONTENT
SETTINGS_EXAMPLE = payload_manifest.EXAMPLE_SETTINGS
SETTINGS_FILE = payload_manifest.SETTINGS_FILE
NETWORKD_PRESET = payload_manifest.NETWORKD_PRESET
NETWORKD_PRESET_CONTENT = payload_manifest.NETWORKD_PRESET_CONTENT
REQUIRED_INSTALLER_TOOLS = ("sfdisk", "lsblk", "findmnt", "mkfs.ext4", "mkfs.fat", "cryptsetup", "tar", "zstd", "mkinitcpio", "lsinitcpio", "systemd-nspawn", "python3", "git", "visudo", "sudo", "partprobe", "vcgencmd", "debugfs", "wipefs", "udevadm")
REQUIRED_FONT = "usr/share/fonts/TTF/DejaVuSansMono.ttf"
# The service launcher runs as the installer user. Verify every directory in
# its staged path is traversable even when the image was assembled under a
# restrictive umask; this is deliberately an explicit payload list rather
# than a recursive permission rewrite.
PUBLIC_PAYLOAD_DIRECTORIES = payload_manifest.PUBLIC_PAYLOAD_DIRECTORIES
EXECUTABLE_PAYLOAD_FILES = payload_manifest.EXECUTABLE_PAYLOAD_FILES

PROVENANCE_OWNER_UID = 0
PROVENANCE_MODULE_FILES = payload_manifest.PROVENANCE_MODULE_FILES

SYSTEM_UNITS = tuple(payload_manifest.SYSTEM_UNITS)
USER_UNITS = tuple(payload_manifest.USER_UNITS)
EXPECTED_LINKS = payload_manifest.EXPECTED_LINKS


class ImageVerificationError(RuntimeError):
    """A completed installer image failed a read-only verification gate."""


@dataclass(frozen=True, slots=True)
class ImageVerificationResult:
    image: Path
    image_bytes: int
    boot_uuid: str
    root_uuid: str
    kernel: str


Runner = Callable[..., Any]


def _absolute(path: Path) -> Path:
    if path.is_absolute():
        return Path(os.path.normpath(os.fspath(path)))
    return Path(os.path.normpath(os.path.join(os.getcwd(), os.fspath(path))))


def _reject_symlink_components(path: Path, *, include_leaf: bool = True) -> None:
    candidate = _absolute(path)
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
            raise ImageVerificationError(f"refusing symlink path component: {current}")


def validate_image_file(path: Path) -> Path:
    """Require a nonempty regular image file with no symlink traversal."""

    candidate = _absolute(path)
    _reject_symlink_components(candidate)
    for protected in (Path("/dev"), Path("/proc"), Path("/sys"), Path("/run")):
        try:
            candidate.relative_to(protected)
        except ValueError:
            continue
        raise ImageVerificationError(f"refusing image path under {protected}")
    try:
        info = candidate.lstat()
    except OSError:
        raise ImageVerificationError("image file is unavailable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ImageVerificationError("image must be a regular file, not a device or symlink")
    if info.st_size <= 0:
        raise ImageVerificationError("image file is empty")
    if not os.access(candidate, os.R_OK):
        raise ImageVerificationError("image file is not readable")
    return candidate


def _run(runner: Runner, command: Sequence[str], **kwargs: Any) -> Any:
    try:
        return runner(list(command), check=True, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ImageVerificationError(f"command failed: {command[0]}") from exc


def _regular_file(path: Path, *, description: str, nonempty: bool = True) -> Path:
    try:
        info = path.lstat()
    except OSError:
        raise ImageVerificationError(f"{description} is missing") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ImageVerificationError(f"{description} must be a regular file")
    if nonempty and info.st_size == 0:
        raise ImageVerificationError(f"{description} is empty")
    return path


def _read_text(path: Path, *, description: str) -> str:
    _regular_file(path, description=description, nonempty=False)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ImageVerificationError(f"{description} is unreadable") from None


def _root_path(root: Path, relative: str, *, allow_leaf_symlink: bool = False) -> Path:
    path = root / relative
    current = root
    components = Path(relative).parts
    for index, component in enumerate(components):
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            if allow_leaf_symlink and index == len(components) - 1:
                continue
            raise ImageVerificationError(f"image path contains an unexpected symlink: {relative}")
    return path


def _require_directory(path: Path, *, description: str) -> Path:
    try:
        info = path.lstat()
    except OSError:
        raise ImageVerificationError(f"{description} is missing") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ImageVerificationError(f"{description} must be a real directory")
    return path


def _validate_partition_table(runner: Runner, image: Path) -> None:
    result = _run(runner, ["sfdisk", "--json", os.fspath(image)], capture_output=True, text=True)
    try:
        document = json.loads(getattr(result, "stdout", ""))
        table = document["partitiontable"]
        if not isinstance(table, dict):
            raise TypeError
        partitions = table["partitions"]
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ImageVerificationError("image has an unreadable DOS partition table") from None
    if str(table.get("label", "")).lower() != "dos":
        raise ImageVerificationError("image partition table is not DOS/MBR")
    if not isinstance(partitions, list) or len(partitions) != 2:
        raise ImageVerificationError("image must contain exactly two partitions")
    first, second = partitions
    try:
        first_start = int(first["start"])
        first_size = int(first["size"])
        second_start = int(second["start"])
        second_size = int(second["size"])
        first_type = str(first["type"]).lower().removeprefix("0x")
        second_type = str(second["type"]).lower().removeprefix("0x")
    except (KeyError, TypeError, ValueError):
        raise ImageVerificationError("image partition table has incomplete entries") from None
    if first_start != FIRST_PARTITION_SECTOR or first_size <= 0 or second_size <= 0:
        raise ImageVerificationError("image partitions have an invalid start or size")
    if second_start != first_start + first_size:
        raise ImageVerificationError("image partitions are not contiguous")
    if first_type != BOOT_TYPE or first.get("bootable") is not True:
        raise ImageVerificationError("image first partition is not bootable FAT32")
    if second_type != ROOT_TYPE:
        raise ImageVerificationError("image second partition is not Linux type 83")


def _filesystem_value(runner: Runner, device: str, field: str) -> str:
    result = _run(runner, ["blkid", "-s", field, "-o", "value", device], capture_output=True, text=True)
    value = str(getattr(result, "stdout", "")).strip()
    if not value:
        raise ImageVerificationError(f"{device} has no filesystem {field}")
    return value


def _validate_filesystems(runner: Runner, boot_device: str, root_device: str) -> tuple[str, str]:
    boot_type = _filesystem_value(runner, boot_device, "TYPE").lower()
    root_type = _filesystem_value(runner, root_device, "TYPE").lower()
    if boot_type not in {"vfat", "fat", "msdos"}:
        raise ImageVerificationError("image first filesystem is not FAT")
    if root_type != "ext4":
        raise ImageVerificationError("image second filesystem is not ext4")
    boot_uuid = _filesystem_value(runner, boot_device, "UUID")
    root_uuid = _filesystem_value(runner, root_device, "UUID")
    if not UUID_PATTERN.fullmatch(boot_uuid) or not UUID_PATTERN.fullmatch(root_uuid):
        raise ImageVerificationError("image filesystem UUID is invalid")
    if boot_uuid.lower() == root_uuid.lower():
        raise ImageVerificationError("image filesystem UUIDs must be distinct")
    return boot_uuid, root_uuid


def _loop_inventory(runner: Runner) -> dict[str, tuple[str, bool]]:
    """Return the safe-to-compare loop inventory from util-linux JSON output.

    ``losetup --find --show`` normally returns the loop name directly.  Keep a
    before/after inventory as a recovery path for a successful invocation with
    malformed stdout, so cleanup never guesses which loop device to detach.
    """

    result = _run(
        runner,
        ["losetup", "--list", "--json", "--output", "NAME,BACK-FILE,RO"],
        capture_output=True,
        text=True,
    )
    try:
        document = json.loads(getattr(result, "stdout", ""))
        entries = document["loopdevices"]
        if not isinstance(entries, list):
            raise TypeError
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ImageVerificationError("losetup returned an unreadable loop inventory") from None
    inventory: dict[str, tuple[str, bool]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ImageVerificationError("losetup returned an invalid loop inventory entry")
        name = entry.get("name")
        back_file = entry.get("back-file")
        read_only = entry.get("ro")
        # Hosts may expose stale entries such as ``/dev/loop1 (lost)``.  They
        # cannot be a candidate for cleanup and must not make an otherwise
        # usable inventory unusable; malformed matching entries are likewise
        # ignored rather than guessed at.
        if not isinstance(name, str) or not LOOP_DEVICE_PATTERN.fullmatch(name):
            continue
        if not isinstance(back_file, str) or not isinstance(read_only, bool):
            continue
        inventory[name] = (back_file, read_only)
    return inventory


def _recover_new_loop(
    runner: Runner,
    image: Path,
    before: dict[str, tuple[str, bool]],
) -> str | None:
    """Identify exactly one newly-created read-only loop for ``image``."""

    after = _loop_inventory(runner)
    image_path = os.path.normpath(os.fspath(image))
    candidates = [
        name
        for name, (back_file, read_only) in after.items()
        if name not in before
        and read_only
        and os.path.normpath(back_file) == image_path
    ]
    return candidates[0] if len(candidates) == 1 else None


def _assert_no_fat_links(boot: Path) -> None:
    for directory, subdirectories, files in os.walk(boot, followlinks=False):
        for name in (*subdirectories, *files):
            candidate = Path(directory) / name
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ImageVerificationError(f"boot filesystem contains a symlink: {candidate.relative_to(boot)}")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ImageVerificationError(f"boot filesystem contains a special file: {candidate.relative_to(boot)}")


def _is_arm64_image(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            header = stream.read(ARM64_IMAGE_MAGIC_OFFSET + len(ARM64_IMAGE_MAGIC))
    except OSError:
        return False
    return header[ARM64_IMAGE_MAGIC_OFFSET : ARM64_IMAGE_MAGIC_OFFSET + len(ARM64_IMAGE_MAGIC)] == ARM64_IMAGE_MAGIC


def _select_dtb(boot: Path) -> Path:
    for relative in DTB_CANDIDATES:
        candidate = boot / relative
        if os.path.lexists(candidate):
            return _regular_file(candidate, description="Pi 5/CM5 device tree")
    expected = ", ".join(os.fspath(item) for item in DTB_CANDIDATES)
    raise ImageVerificationError(f"Pi 5/CM5 device tree is missing (expected one of {expected})")


def _verify_boot(boot: Path, root_uuid: str, example: Path) -> str:
    _require_directory(boot, description="boot filesystem")
    _assert_no_fat_links(boot)
    example_file = _regular_file(boot / SETTINGS_EXAMPLE, description="settings example")
    try:
        expected_example = example.read_bytes()
        actual_example = example_file.read_bytes()
    except OSError:
        raise ImageVerificationError("settings example is unreadable") from None
    if actual_example != expected_example:
        raise ImageVerificationError("settings example differs from the reviewed source")
    if os.path.lexists(boot / SETTINGS_FILE):
        raise ImageVerificationError("private installer-settings.toml is present in the image")

    config = _read_text(boot / "config.txt", description="boot config.txt")
    cmdline = _read_text(boot / "cmdline.txt", description="boot cmdline.txt")
    lines = [line.strip() for line in config.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    kernels = [line.split("=", 1)[1].strip() for line in lines if line.lower().startswith("kernel=") and "=" in line]
    if len(kernels) != 1 or kernels[0] not in KERNEL_NAMES:
        raise ImageVerificationError("config.txt must select exactly one supported Pi kernel")
    kernel = _regular_file(boot / kernels[0], description="selected Pi kernel")
    if not _is_arm64_image(kernel):
        raise ImageVerificationError("selected Pi kernel is not an ARM64 Linux Image")
    if sum(line.lower() == "initramfs initramfs-linux.img followkernel" for line in lines) != 1:
        raise ImageVerificationError("config.txt must select initramfs-linux.img with followkernel")
    if sum(line.lower() == "dtoverlay=vc4-kms-v3d-pi5" for line in lines) != 1:
        raise ImageVerificationError("config.txt must select the Pi 5 DRM overlay")
    if sum(line.lower() == "dtparam=pciex1_gen=1" for line in lines) != 1:
        raise ImageVerificationError("config.txt must select PCIe Gen1")
    if "[pi5]" not in {line.lower() for line in lines} or "[all]" not in {line.lower() for line in lines}:
        raise ImageVerificationError("config.txt is missing required Pi sections")
    _regular_file(boot / "initramfs-linux.img", description="installer initramfs")
    _select_dtb(boot)
    _regular_file(boot / "overlays/vc4-kms-v3d-pi5.dtbo", description="Pi 5 DRM overlay")

    tokens = cmdline.split()
    root_tokens = [token for token in tokens if token.startswith("root=")]
    if root_tokens != [f"root=UUID={root_uuid}"]:
        raise ImageVerificationError("cmdline.txt has an unexpected root filesystem selector")
    if "rootfstype=ext4" not in tokens or "rootwait" not in tokens:
        raise ImageVerificationError("cmdline.txt is missing ext4/rootwait arguments")
    if any(LUKS_ARGUMENT.match(token) for token in tokens):
        raise ImageVerificationError("cmdline.txt contains a LUKS argument")
    return kernels[0]


def _verify_fstab(root: Path, boot_uuid: str, root_uuid: str) -> None:
    text = _read_text(root / "etc/fstab", description="target fstab")
    entries: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) < 3 or fields[1] not in {"/", "/boot"}:
            continue
        if fields[1] in entries:
            raise ImageVerificationError(f"fstab has duplicate {fields[1]} entry")
        entries[fields[1]] = (fields[0], fields[2])
    if entries.get("/") != (f"UUID={root_uuid}", "ext4"):
        raise ImageVerificationError("fstab root entry does not match the image UUID")
    if entries.get("/boot") != (f"UUID={boot_uuid}", "vfat"):
        raise ImageVerificationError("fstab boot entry does not match the image UUID")


def _verify_installer_provenance(root: Path) -> None:
    path = _root_path(root, "usr/lib/omarchy-pi/installer-provenance.json")
    info = _regular_file(path, description="installer provenance").stat()
    if info.st_uid != PROVENANCE_OWNER_UID or stat.S_IMODE(info.st_mode) != 0o644:
        raise ImageVerificationError("installer provenance owner or mode is incorrect")
    try:
        data = json.loads(path.read_text())
        if set(data) != {"schema_version", "source_revision", "runtime_sha256", "files"} or data["schema_version"] != 1:
            raise ValueError
        revision = data["source_revision"]
        if revision is not None and not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError
        files = data["files"]
        if not isinstance(files, dict) or not files:
            raise ValueError
        expected_files = ((set(EXECUTABLE_PAYLOAD_FILES + PROVENANCE_MODULE_FILES) - {"usr/bin/hypr-rdp"})
                          | {"usr/local/share/omarchy-pi/" + name for name in payload_manifest.SHARE_FILES}
                          | {"etc/systemd/system/" + name for name in SYSTEM_UNITS}
                          | {"etc/systemd/user/" + name for name in USER_UNITS})
        # Every runtime entrypoint and module must have a content hash. Other
        # staged units/configuration are also included in the descriptor.
        if set(files) != expected_files:
            raise ValueError
        for relative, digest in files.items():
            if not isinstance(relative, str) or relative.startswith("/") or ".." in relative.split("/"):
                raise ValueError
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError
            payload = _regular_file(_root_path(root, relative), description="installer provenance file")
            file_info = payload.stat()
            if file_info.st_uid != PROVENANCE_OWNER_UID or file_info.st_mode & 0o022:
                raise ValueError
            if hashlib.sha256(payload.read_bytes()).hexdigest() != digest:
                raise ValueError
        actual = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if data["runtime_sha256"] != actual:
            raise ValueError
    except (ValueError, TypeError, KeyError, OSError):
        raise ImageVerificationError("installer provenance does not match the staged runtime") from None


def _verify_identities(root: Path) -> None:
    for relative in ("etc/machine-id", "var/lib/dbus/machine-id", "installer-settings.toml", "boot/installer-settings.toml"):
        if os.path.lexists(_root_path(root, relative)):
            raise ImageVerificationError(f"cloned or private identity file is present: {relative}")
    ssh_dir = _root_path(root, "etc/ssh")
    if not os.path.lexists(ssh_dir):
        return
    _require_directory(ssh_dir, description="target SSH directory")
    for entry in ssh_dir.iterdir():
        if entry.name.startswith("ssh_host_"):
            raise ImageVerificationError("cloned SSH host keys are present")


def _verify_services(root: Path) -> None:
    for relative in PUBLIC_PAYLOAD_DIRECTORIES:
        directory = _require_directory(
            _root_path(root, relative),
            description=f"installer payload directory {relative}",
        )
        if stat.S_IMODE(directory.lstat().st_mode) != 0o755:
            raise ImageVerificationError(f"installer payload directory is not mode 0755: {relative}")
    for relative in EXECUTABLE_PAYLOAD_FILES:
        executable = _regular_file(
            _root_path(root, relative),
            description=f"installer executable {relative}",
        )
        if stat.S_IMODE(executable.lstat().st_mode) != 0o755:
            raise ImageVerificationError(f"installer executable is not mode 0755: {relative}")
    for tool in REQUIRED_INSTALLER_TOOLS:
        path = _root_path(root, "usr/bin/" + tool, allow_leaf_symlink=True)
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ImageVerificationError("installer utility is missing or escapes target: " + tool)
    marker = _root_path(root, os.fspath(BUILDER_MARKER))
    _regular_file(marker, description="installer image marker")
    if marker.read_bytes() != BUILDER_MARKER_CONTENT:
        raise ImageVerificationError("installer image marker is invalid")
    for module in PROVENANCE_MODULE_FILES:
        _regular_file(_root_path(root, module), description="installer runtime module")
    worker_wants = _root_path(root, "etc/systemd/system/multi-user.target.wants/omarchy-pi-install.service", allow_leaf_symlink=True)
    if os.path.lexists(worker_wants):
        raise ImageVerificationError("destructive install worker must not start at boot")
    for unit in SYSTEM_UNITS:
        _regular_file(_root_path(root, f"etc/systemd/system/{unit}"), description=f"system unit {unit}")
    for unit in USER_UNITS:
        _regular_file(_root_path(root, f"etc/systemd/user/{unit}"), description=f"user unit {unit}")
    firstboot_mask = _root_path(
        root,
        "etc/systemd/system/systemd-firstboot.service",
        allow_leaf_symlink=True,
    )
    if not firstboot_mask.is_symlink() or os.readlink(firstboot_mask) != "/dev/null":
        raise ImageVerificationError(
            "interactive systemd-firstboot.service is not masked"
        )
    for relative, expected in EXPECTED_LINKS.items():
        path = _root_path(root, relative, allow_leaf_symlink=True)
        if not path.is_symlink() or os.readlink(path) != expected:
            raise ImageVerificationError(f"service link is missing or unexpected: {relative}")
        if expected.startswith("/"):
            destination = _root_path(root, expected.lstrip("/"))
        else:
            destination = path.parent / expected
        if not os.path.exists(destination):
            raise ImageVerificationError(f"service link target is missing: {expected}")


def _verify_fonts(root: Path) -> None:
    _regular_file(_root_path(root, REQUIRED_FONT), description="DejaVu Sans Mono font")


def _verify_networkd_preset(root: Path) -> None:
    preset = _regular_file(_root_path(root, NETWORKD_PRESET), description="installer networkd preset")
    if preset.read_bytes() != NETWORKD_PRESET_CONTENT:
        raise ImageVerificationError("installer networkd preset is invalid")


def _verify_root(root: Path, boot_uuid: str, root_uuid: str) -> None:
    _require_directory(root, description="root filesystem")
    _require_directory(_root_path(root, "boot"), description="target /boot directory")
    _verify_fstab(root, boot_uuid, root_uuid)
    _verify_identities(root)
    _verify_fonts(root)
    _verify_networkd_preset(root)
    _verify_services(root)
    _verify_installer_provenance(root)
    try:
        metadata = desktop_payload.payload_metadata(
            root / desktop_payload.BUNDLE.relative_to("/"),
            root / desktop_payload.DESCRIPTOR.relative_to("/"),
        )
    except (OSError, ValueError, desktop_payload.PayloadError) as exc:
        raise ImageVerificationError("desktop bundle is absent or invalid") from exc
    filesystem = os.statvfs(root)
    if filesystem.f_bavail * filesystem.f_frsize < metadata["unpacked_bytes"] + 256 * 1024 * 1024:
        raise ImageVerificationError("installer root has no desktop unpacking allowance")


class _MountedImage:
    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self.directory: Path | None = None
        self.root_mount: Path | None = None
        self.boot_mount: Path | None = None
        self.loop_device: str | None = None
        self.root_mounted = False
        self.boot_mounted = False

    def __enter__(self) -> "_MountedImage":
        try:
            self.directory = Path(tempfile.mkdtemp(prefix="omarchy-image-verify-"))
            self.root_mount = self.directory / "root"
            self.boot_mount = self.directory / "boot"
            self.root_mount.mkdir()
            self.boot_mount.mkdir()
            return self
        except BaseException:
            failures = self.close()
            if failures:
                raise ImageVerificationError("cleanup failed (" + ", ".join(failures) + ")") from None
            raise

    def attach(self, image: Path) -> tuple[str, str]:
        # Capture existing loop names first.  If losetup succeeds but emits
        # malformed stdout, only a loop that is both new and backed by this
        # exact image is safe to hand to close(); never detach by guessing.
        before_loops = _loop_inventory(self.runner)
        result = _run(
            self.runner,
            ["losetup", "--find", "--show", "--partscan", "--read-only", os.fspath(image)],
            capture_output=True,
            text=True,
        )
        loop = str(getattr(result, "stdout", "")).strip()
        if not LOOP_DEVICE_PATTERN.fullmatch(loop):
            try:
                recovered = _recover_new_loop(self.runner, image, before_loops)
            except ImageVerificationError:
                recovered = None
            if recovered is not None:
                self.loop_device = recovered
            raise ImageVerificationError("losetup returned an unsafe loop device")
        self.loop_device = loop
        # The image assembler uses /dev/loopNp1 and /dev/loopNp2.  The exact
        # node names are checked against the safe loop path above.
        boot_device = f"{loop}p1"
        root_device = f"{loop}p2"
        boot_uuid, root_uuid = _validate_filesystems(self.runner, boot_device, root_device)
        _run(self.runner, ["mount", "--read-only", "-o", "noload", "--", root_device, os.fspath(self.root_mount)])
        self.root_mounted = True
        _run(self.runner, ["mount", "--read-only", "-t", "vfat", "--", boot_device, os.fspath(self.boot_mount)])
        self.boot_mounted = True
        return boot_uuid, root_uuid

    def close(self) -> list[str]:
        failures: list[str] = []
        if self.boot_mounted and self.boot_mount is not None:
            try:
                _run(self.runner, ["umount", "--", os.fspath(self.boot_mount)])
                self.boot_mounted = False
            except ImageVerificationError:
                failures.append("boot unmount")
        if self.root_mounted and self.root_mount is not None:
            try:
                _run(self.runner, ["umount", "--", os.fspath(self.root_mount)])
                self.root_mounted = False
            except ImageVerificationError:
                failures.append("root unmount")
        if self.loop_device is not None and not self.boot_mounted and not self.root_mounted:
            try:
                _run(self.runner, ["losetup", "--detach", self.loop_device])
                self.loop_device = None
            except ImageVerificationError:
                failures.append("loop detach")
        if not failures and self.directory is not None:
            try:
                shutil.rmtree(self.directory, ignore_errors=False)
            except OSError:
                failures.append("temporary directory removal")
            else:
                self.directory = None
        return failures

    def __exit__(self, exc_type: object, exc_value: BaseException | None, traceback: object) -> bool:
        failures = self.close()
        if failures:
            detail = "cleanup failed (" + ", ".join(failures) + ")"
            if exc_value is not None:
                raise ImageVerificationError(detail) from exc_value
            raise ImageVerificationError(detail)
        return False


def verify_image(
    image: Path,
    *,
    example: Path | None = None,
    runner: Runner = subprocess.run,
) -> ImageVerificationResult:
    """Verify one completed image, using only read-only mounts."""

    image_path = validate_image_file(image)
    example_path = example or Path(__file__).with_name(SETTINGS_EXAMPLE)
    _reject_symlink_components(example_path)
    _regular_file(example_path, description="reviewed settings example")
    _validate_partition_table(runner, image_path)
    with _MountedImage(runner) as mounted:
        boot_uuid, root_uuid = mounted.attach(image_path)
        assert mounted.root_mount is not None and mounted.boot_mount is not None
        kernel = _verify_boot(mounted.boot_mount, root_uuid, example_path)
        _verify_root(mounted.root_mount, boot_uuid, root_uuid)
    return ImageVerificationResult(image_path, image_path.stat().st_size, boot_uuid, root_uuid, kernel)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True, help="completed regular image file")
    parser.add_argument(
        "--settings-example",
        type=Path,
        default=None,
        help="reviewed installer settings example; defaults to this directory",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = verify_image(args.image, example=args.settings_example)
    except (ImageVerificationError, OSError, subprocess.CalledProcessError) as exc:
        print(f"verify-installer-image: error: {exc}", file=os.sys.stderr)
        return 2
    print(
        f"verified {result.image} ({result.image_bytes} bytes); "
        f"boot UUID {result.boot_uuid}; root UUID {result.root_uuid}; kernel {result.kernel}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
