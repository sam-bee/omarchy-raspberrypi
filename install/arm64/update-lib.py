#!/usr/bin/python3
"""Small, read-only helpers shared by the Raspberry Pi updater.

The updater deliberately records facts instead of trying to make a system look
like a new installation.  In particular, the protected-state snapshot is a
comparison aid; it never writes boot files, credentials, EEPROM or network
configuration.
"""

from __future__ import annotations

import hashlib
import gzip
import json
import os
import platform
import shutil
import re
import subprocess
import time
import struct
import fcntl
from pathlib import Path
from typing import Any, Iterable


PROTECTED_FILES = (
    "/boot/config.txt",
    "/boot/cmdline.txt",
    "/etc/fstab",
    "/etc/crypttab",
    "/etc/mkinitcpio.conf",
    "/etc/ssh/sshd_config",
    "/etc/pacman.conf",
    "/etc/pacman.d/mirrorlist",
    "/etc/sudoers",
    "/etc/omarchy-pi/rdp-profile.toml",
)
PROTECTED_DIRS = (
    "/etc/mkinitcpio.conf.d",
    "/etc/NetworkManager/system-connections",
    "/etc/wpa_supplicant",
    "/etc/ssh",
    "/etc/sudoers.d",
)
BOOT_KERNEL_GLOBS = ("kernel8.img", "kernel_2712.img", "vmlinuz*", "initramfs*", "*.dtb")
CUSTOM_PACKAGES = ("hypr-rdp", "ttfx")
GET_REBOOT_ORDER = 0x0003008B
GET_GENCMD_RESULT = 0x00030080
RESPONSE_OK = 0x80000000
RESPONSE_BYTES = 0x80000004
EXPECTED_MODULES = {"nvme", "xhci_pci", "usb_storage", "uas", "usbhid", "hid_generic", "mmc_core", "mmc_block", "ext4"}
EXPECTED_HOOKS = {"base", "systemd", "modconf", "keyboard", "sd-vconsole", "block", "filesystems", "fsck"}


class UpdateCheckError(RuntimeError):
    """A protected-state or package preflight could not be completed."""


def root_path(root: str | Path, absolute: str) -> Path:
    """Map an absolute target path into an optional offline fixture root."""

    base = Path(root)
    if absolute == "/":
        return base
    return base / absolute.lstrip("/")


def state_path(state_dir: str | Path, name: str) -> Path:
    path = Path(state_dir) / name
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def atomic_json(path: str | Path, value: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)


def load_json(path: str | Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, OSError, ValueError):
        return default


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def path_record(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if path.is_file():
        stat = path.stat()
        return {"kind": "file", "sha256": digest(path), "size": stat.st_size}
    if path.is_dir():
        return {"kind": "directory"}
    return {"kind": "missing"}


def directory_record(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_dir() or path.is_symlink():
        return path_record(path)
    records: dict[str, Any] = {}
    for child in sorted(path.rglob("*")):
        if child.is_file() or child.is_symlink():
            records[str(child.relative_to(path))] = path_record(child)
    return {"kind": "directory", "entries": records}


def command_output(command: list[str], *, timeout: float = 15) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return completed.returncode, completed.stdout


def package_db() -> dict[str, str]:
    fixture = os.environ.get("OMARCHY_PI_PACKAGE_DB")
    if fixture:
        loaded = load_json(fixture)
        if not isinstance(loaded, dict):
            raise UpdateCheckError(f"package fixture is not a JSON object: {fixture}")
        return {str(name): str(version) for name, version in loaded.items()}
    pacman = os.environ.get("OMARCHY_PI_PACMAN", "pacman")
    status, output = command_output([pacman, "-Q"])
    if status != 0:
        raise UpdateCheckError(f"could not read the installed package database with {pacman}")
    packages: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            packages[fields[0]] = fields[1]
    if not packages:
        raise UpdateCheckError("pacman returned an empty installed package database")
    return packages


def package_versions(names: Iterable[str] = CUSTOM_PACKAGES) -> dict[str, str | None]:
    db = package_db()
    return {name: db.get(name) for name in names}


def is_raspberry_pi(root: str | Path = "/") -> bool:
    if str(root) != "/":
        return False
    for path in (Path("/proc/device-tree/model"), Path("/sys/firmware/devicetree/base/model")):
        try:
            if "raspberry pi" in path.read_bytes().decode(errors="ignore").lower():
                return True
        except OSError:
            pass
    return False


def bootloader_record(root: str | Path = "/") -> dict[str, Any]:
    """Read EEPROM state without invoking an update utility.

    ``vcgencmd`` is preferred because it is the Raspberry Pi supported
    interface.  Some Arch ARM images do not package it, so the fallback sends
    only the read-only ``GET_GENCMD_RESULT`` property through the firmware
    mailbox.  It never sends a SET tag and never invokes rpi-eeprom tooling.
    """

    if str(root) != "/":
        return {"method": "fixture", "output": None}
    command = shutil.which("vcgencmd")
    if command is not None:
        status, output = command_output([command, "bootloader_config"], timeout=10)
        if status == 0 and output.strip() and "boot_order" in output.lower():
            return {"method": "vcgencmd bootloader_config", "output": output}
    output = _vcio_bootloader_config()
    reboot_order = _vcio_reboot_order()
    if output and reboot_order is not None and "boot_order" in output.lower():
        return {
            "method": "/dev/vcio GET_GENCMD_RESULT",
            "output": output,
            "reboot_order": f"0x{reboot_order:08x}",
        }
    if is_raspberry_pi(root):
        raise UpdateCheckError("could not read Raspberry Pi EEPROM bootloader_config via vcgencmd or /dev/vcio")
    return {"method": None, "output": None}


def _vcio_bootloader_config() -> str | None:
    """Read ``bootloader_config`` using the firmware's GET gencmd property.

    The ioctl is the upstream ``IOCTL_MBOX_PROPERTY`` definition.  The
    command buffer contains only tag ``0x00030080`` (GET_GENCMD_RESULT) and
    the literal read-only command; no EEPROM write tag can be sent here.
    """

    try:
        devices = ("/dev/vcio_gencmd", "/dev/vcio")
        pointer_size = struct.calcsize("P")
        ioctl_number = (3 << 30) | (pointer_size << 16) | (100 << 8)
        max_string = 4 * 1024
        word_count = 6 + (max_string // 4) + 1
        command = b"bootloader_config\0"
    except (OSError, ValueError, struct.error):
        return None
    for device in devices:
        descriptor: int | None = None
        try:
            message = bytearray(word_count * 4)
            struct.pack_into("<6I", message, 0, len(message), 0, GET_GENCMD_RESULT, max_string, 0, 0)
            message[24 : 24 + len(command)] = command
            struct.pack_into("<I", message, 24 + max_string, 0)
            descriptor = os.open(
                device,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            fcntl.ioctl(descriptor, ioctl_number, message, True)
            total, status, tag, buffer_size, response_length, command_status = struct.unpack_from("<6I", message, 0)
            response_size = response_length & 0x7FFFFFFF
            if (
                total != len(message)
                or status != 0x80000000
                or tag != GET_GENCMD_RESULT
                or buffer_size != max_string
                or not (response_length & 0x80000000)
                or response_size > max_string
                or command_status != 0
                or struct.unpack_from("<I", message, 24 + max_string)[0] != 0
            ):
                continue
            payload = bytes(message[24 : 24 + max_string])
            nul = payload.find(b"\0")
            if nul < 0 or nul > response_size or any(payload[nul + 1 :]):
                continue
            try:
                text = payload[:nul].decode("ascii").strip()
            except UnicodeDecodeError:
                continue
            if text:
                return text + "\n"
        except (OSError, ValueError):
            continue
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
    return None


def _vcio_reboot_order() -> int | None:
    """Read the one-shot reboot-order property through the same GET-only path."""

    for device in ("/dev/vcio_gencmd", "/dev/vcio"):
        descriptor: int | None = None
        message = bytearray(7 * 4)
        struct.pack_into("<7I", message, 0, len(message), 0, GET_REBOOT_ORDER, 4, 4, 0, 0)
        try:
            descriptor = os.open(
                device,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            fcntl.ioctl(descriptor, (3 << 30) | (struct.calcsize("P") << 16) | (100 << 8), message, True)
            total, status, tag, buffer_size, response_length, order = struct.unpack_from("<6I", message, 0)
            terminator = struct.unpack_from("<I", message, 24)[0]
            if (
                total == len(message)
                and status == RESPONSE_OK
                and tag == GET_REBOOT_ORDER
                and buffer_size == 4
                and response_length == RESPONSE_BYTES
                and terminator == 0
            ):
                return order
        except (OSError, ValueError, struct.error):
            pass
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
    return None


def parse_mkinitcpio_config(root: str | Path = "/") -> dict[str, Any]:
    """Read the reviewed Pi module/hook contract without evaluating shell."""

    files: list[Path] = []
    primary = root_path(root, "/etc/mkinitcpio.conf")
    if primary.is_file():
        files.append(primary)
    directory = root_path(root, "/etc/mkinitcpio.conf.d")
    if directory.is_dir() and not directory.is_symlink():
        files.extend(sorted(directory.glob("*.conf")))
    text = "\n".join(path.read_text(errors="replace") for path in files)
    values: dict[str, set[str]] = {"MODULES": set(), "HOOKS": set()}
    for name in values:
        matches = re.findall(rf"(?m)^\s*{name}\s*=\s*\(([^)]*)\)", text)
        for match in matches:
            values[name].update(re.findall(r"[A-Za-z0-9_.+-]+", match))
    presets: dict[str, dict[str, str]] = {}
    preset_dir = root_path(root, "/etc/mkinitcpio.d")
    if preset_dir.is_dir() and not preset_dir.is_symlink():
        for path in sorted(preset_dir.glob("*.preset")):
            preset_text = path.read_text(errors="replace")
            values_for_preset = dict(
                re.findall(r"(?m)^\s*((?:ALL_)?kver|[A-Za-z0-9_]+_(?:image|kver))\s*=\s*['\"]([^'\"]+)", preset_text)
            )
            if values_for_preset:
                presets[str(path.relative_to(root_path(root, "/")))] = values_for_preset
    return {"modules": sorted(values["MODULES"]), "hooks": sorted(values["HOOKS"]), "presets": presets}


def _module_root(root: str | Path) -> Path:
    for candidate in (root_path(root, "/usr/lib/modules"), root_path(root, "/lib/modules")):
        if candidate.is_dir() and not candidate.is_symlink():
            return candidate
    return root_path(root, "/usr/lib/modules")


def _kernel_versions(root: str | Path) -> dict[str, dict[str, Any]]:
    modules = _module_root(root)
    result: dict[str, dict[str, Any]] = {}
    if not modules.is_dir() or modules.is_symlink():
        return result
    for entry in sorted(modules.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        pkgbase = entry / "pkgbase"
        result[entry.name] = {
            "pkgbase": pkgbase.read_text(errors="replace").strip() if pkgbase.is_file() else None,
            "modules_builtin": path_record(entry / "modules.builtin"),
        }
    return result


def _kernel_package_files(root: str | Path) -> dict[str, Any]:
    """Record whether installed linux-rpi metadata owns the Pi boot files."""

    local = root_path(root, "/var/lib/pacman/local")
    if not local.is_dir() or local.is_symlink():
        return {}
    result: dict[str, Any] = {
        "kernel8.img": False,
        "kernel_2712.img": False,
        "kernel8.img.sha256": None,
        "kernel_2712.img.sha256": None,
    }
    for record in sorted(local.glob("linux-rpi-*/files")):
        if not record.is_file() or record.is_symlink():
            continue
        in_files = False
        try:
            lines = record.read_text(errors="replace").splitlines()
        except OSError as exc:
            raise UpdateCheckError(f"linux-rpi package file list is unreadable: {record}") from exc
        for line in lines:
            if line == "%FILES%":
                in_files = True
                continue
            if in_files and line.startswith("%"):
                break
            if in_files:
                normalized = line.lstrip("/")
                if normalized in {"boot/kernel8.img", "boot/kernel_2712.img"}:
                    result[Path(normalized).name] = True
        mtree = record.with_name("mtree")
        if mtree.is_file() and not mtree.is_symlink():
            try:
                raw_mtree = mtree.read_bytes()
                if raw_mtree.startswith(b"\x1f\x8b"):
                    raw_mtree = gzip.decompress(raw_mtree)
                mtree_text = raw_mtree.decode(errors="replace")
            except (OSError, EOFError, gzip.BadGzipFile, UnicodeError) as exc:
                raise UpdateCheckError(f"linux-rpi package mtree is unreadable: {mtree}") from exc
            for image in ("kernel8.img", "kernel_2712.img"):
                match = re.search(
                    rf"(?:^|\s)\.?/?boot/{re.escape(image)}\s+[^\n]*?sha256digest=([0-9a-fA-F]{{64}})",
                    mtree_text,
                    re.MULTILINE,
                )
                if match:
                    result[f"{image}.sha256"] = match.group(1).lower()
    return result


def _selected_kernel(root: str | Path) -> str:
    config = root_path(root, "/boot/config.txt")
    try:
        text = config.read_text(errors="replace")
    except OSError:
        return "kernel8.img"
    for line in text.splitlines():
        match = re.match(r"^\s*kernel\s*=\s*([^\s#]+)", line, re.IGNORECASE)
        if match:
            return Path(match.group(1)).name
    return "kernel8.img"


def _initramfs_listing(root: str | Path, image: str) -> dict[str, Any]:
    """Inspect initramfs contents when the target provides lsinitcpio."""

    if str(root) == "/":
        command = shutil.which("lsinitcpio")
        image_path = image
    else:
        command_path = root_path(root, "/usr/bin/lsinitcpio")
        command = str(command_path) if command_path.is_file() and os.access(command_path, os.X_OK) else None
        image_path = str(root_path(root, image))
    if command is None:
        return {"status": "unavailable", "kernel_versions": [], "modules": []}
    status, output = command_output([command, "-l", image_path], timeout=30)
    if status != 0 or not output.strip():
        return {"status": "failed", "kernel_versions": [], "modules": []}
    versions = sorted(
        set(
            re.findall(
                r"(?:^|/)(?:usr/)?lib/modules/([^/\s]+)/",
                output,
            )
        )
    )
    modules = sorted(
        set(
            name.replace("-", "_")
            for name in re.findall(r"(?:^|/)([A-Za-z0-9_.+-]+)\.ko(?:\.[^/\s]+)?(?:\s|$)", output)
        )
    )
    return {"status": "ok", "kernel_versions": versions, "modules": modules}


def kernel_artifacts(root: str | Path, mkinitcpio: dict[str, Any]) -> dict[str, Any]:
    """Capture kernel/module/initramfs linkage facts for post-transaction checks."""

    images: set[str] = set()
    module_versions = _kernel_versions(root)
    preset_kvers: dict[str, str] = {}
    preset_kernel_images: dict[str, str] = {}
    for preset, values in mkinitcpio.get("presets", {}).items():
        for name, value in values.items():
            if name.endswith("_image"):
                images.add(value)
            elif name == "ALL_kver" or name.endswith("_kver"):
                module_match = re.search(r"/(?:usr/)?lib/modules/([^/]+)(?:/|$)", value)
                candidate = module_match.group(1) if module_match else Path(value).name
                if module_match or candidate in module_versions or not value.startswith("/"):
                    preset_kvers[f"{preset}:{name}"] = candidate
                else:
                    preset_kernel_images[f"{preset}:{name}"] = value
    initramfs = {image: _initramfs_listing(root, image) for image in sorted(images)}
    return {
        "kernel8": path_record(root_path(root, "/boot/kernel8.img")),
        "kernel_2712": path_record(root_path(root, "/boot/kernel_2712.img")),
        "selected_kernel": _selected_kernel(root),
        "module_versions": module_versions,
        "preset_kvers": preset_kvers,
        "preset_kernel_images": preset_kernel_images,
        "linux_rpi_boot_files": _kernel_package_files(root),
        "initramfs": initramfs,
    }


def verify_kernel_artifacts(state: dict[str, Any], *, root: str | Path = "/") -> list[str]:
    """Verify that the Pi kernel, module tree and initramfs describe one build."""

    failures: list[str] = []
    artifacts = state.get("kernel_artifacts", {})
    live_pi = str(root) == "/" and is_raspberry_pi(root)
    kernel8 = artifacts.get("kernel8", {})
    if kernel8.get("kind") != "file" or kernel8.get("size", 0) <= 0:
        failures.append("/boot/kernel8.img is missing or empty")
    modules = artifacts.get("module_versions", {})
    preset_kvers = set(artifacts.get("preset_kvers", {}).values())
    preset_kernel_images = artifacts.get("preset_kernel_images", {})
    initramfs = artifacts.get("initramfs", {})
    if live_pi and not modules:
        failures.append("installed kernel module tree is missing")
    if live_pi and not preset_kvers and not preset_kernel_images:
        failures.append("linux-rpi mkinitcpio preset does not identify its kernel modules")
    if live_pi and not initramfs:
        failures.append("linux-rpi mkinitcpio preset does not select an initramfs image")
    for version in sorted(preset_kvers):
        if version not in modules:
            failures.append(f"mkinitcpio preset selects missing kernel modules: {version}")
        elif live_pi and modules[version].get("pkgbase") != "linux-rpi":
            failures.append(f"mkinitcpio preset selects modules not identified as linux-rpi: {version}")
    owned = artifacts.get("linux_rpi_boot_files", {})
    selected_kernel = artifacts.get("selected_kernel", "kernel8.img")
    if live_pi and state.get("packages", {}).get("linux-rpi") and not owned:
        failures.append("linux-rpi package file ownership metadata is unavailable")
    if live_pi and owned and not owned.get(selected_kernel, False):
        failures.append(f"/boot/{selected_kernel} is not owned by the installed linux-rpi package")
    if live_pi and owned:
        expected_hash = owned.get(f"{selected_kernel}.sha256")
        actual_path = root_path(root, "/boot/" + selected_kernel)
        if not expected_hash:
            failures.append(f"installed linux-rpi package mtree has no digest for selected kernel: {selected_kernel}")
        elif not actual_path.is_file() or digest(actual_path) != expected_hash:
            failures.append(f"/boot/{selected_kernel} failed the installed linux-rpi package mtree digest check")
    for image, listing in sorted(initramfs.items()):
        if listing.get("status") != "ok":
            if live_pi:
                failures.append(f"could not inspect initramfs image: {image}")
            continue
        listed_versions = set(listing.get("kernel_versions", []))
        if preset_kvers and not listed_versions.intersection(preset_kvers):
            failures.append(f"initramfs {image} does not contain the preset kernel modules")
        if not preset_kvers and preset_kernel_images and not listed_versions.intersection(modules):
            failures.append(f"initramfs {image} does not contain installed kernel modules")
        if live_pi and not preset_kvers and preset_kernel_images:
            for version in sorted(listed_versions.intersection(modules)):
                if modules[version].get("pkgbase") != "linux-rpi":
                    failures.append(f"initramfs {image} selects modules not identified as linux-rpi: {version}")
    if live_pi:
        kernel_path = root_path(root, "/boot/kernel8.img")
        try:
            with kernel_path.open("rb") as stream:
                stream.seek(0x38)
                arm64_magic = stream.read(4) == b"ARM\x64"
        except OSError:
            arm64_magic = False
        file_status, file_output = command_output(["file", "-Lb", str(kernel_path)]) if not arm64_magic else (0, "")
        if not arm64_magic and (file_status != 0 or ("ELF" not in file_output and not any(token in file_output for token in ("ARM64", "AArch64", "aarch64")))):
            failures.append("/boot/kernel8.img is not an identifiable ARM64 kernel image")
    return failures


def recipe_hashes(source_dir: str | Path | None) -> dict[str, str]:
    if source_dir is None:
        return {}
    root = Path(source_dir)
    result: dict[str, str] = {}
    for package in CUSTOM_PACKAGES:
        recipe = root / "install/arm64/packages" / package / "PKGBUILD"
        if recipe.is_file() and not recipe.is_symlink():
            result[package] = digest(recipe)
    return result


def active_source_dir(home: str | Path) -> Path | None:
    current = Path(home) / ".local/share/omarchy-pi/current"
    try:
        resolved = current.resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def account_records(root: str | Path, home: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Hash only root/target authentication records; never expose shadow text."""

    passwd_path = root_path(root, "/etc/passwd")
    shadow_path = root_path(root, "/etc/shadow")
    accounts: dict[str, str] = {}
    homes: dict[str, str] = {}
    if passwd_path.is_file():
        for line in passwd_path.read_text(errors="replace").splitlines():
            fields = line.split(":")
            if len(fields) >= 6:
                accounts[fields[0]] = fields[5]
                homes[fields[0]] = fields[5]
    target = next((name for name, account_home in homes.items() if account_home == str(home)), None)
    live = str(root) == "/" and os.environ.get("OMARCHY_PI_TESTING") != "1"
    if live and (not passwd_path.is_file() or "root" not in accounts or target is None):
        raise UpdateCheckError("target/root account records are unavailable for access preservation")
    selected = [name for name in ("root", target) if name and name in accounts]
    auth: dict[str, Any] = {}
    if shadow_path.is_file():
        for line in shadow_path.read_text(errors="replace").splitlines():
            fields = line.split(":", 1)
            if len(fields) == 2 and fields[0] in selected:
                auth[fields[0]] = {"sha256": hashlib.sha256(line.encode()).hexdigest()}
    elif str(root) == "/" and os.geteuid() == 0 and os.environ.get("OMARCHY_PI_TESTING") != "1":
        raise UpdateCheckError("/etc/shadow is unavailable for authentication-state preservation")
    if live and any(name not in auth for name in selected):
        raise UpdateCheckError("target/root shadow records are unavailable for authentication-state preservation")
    ssh: dict[str, Any] = {}
    for name in selected:
        ssh_path = root_path(root, homes[name]) / ".ssh"
        ssh[name] = directory_record(ssh_path)
    return auth, ssh


def compare_recipe_hashes(old_release: str | Path, new_release: str | Path) -> list[str]:
    """Return custom-package recipe drift between two prepared releases."""

    old = recipe_hashes(old_release)
    new = recipe_hashes(new_release)
    return [
        f"custom package recipe changed without an explicit review: {package}"
        for package in sorted(set(old) | set(new))
        if old.get(package) != new.get(package)
    ]


def snapshot(*, root: str | Path = "/", home: str | Path | None = None, source_dir: str | Path | None = None) -> dict[str, Any]:
    """Capture state that an update must preserve or revalidate."""

    root = Path(root)
    if home is None:
        home = os.environ.get("HOME", str(Path.home()))
    home_path = Path(home)
    protected = {
        name: path_record(root_path(root, name)) for name in PROTECTED_FILES
    }
    protected_dirs = {
        name: directory_record(root_path(root, name)) for name in PROTECTED_DIRS
    }
    boot = root_path(root, "/boot")
    boot_entries: dict[str, Any] = {}
    if boot.is_dir() and not boot.is_symlink():
        for pattern in BOOT_KERNEL_GLOBS:
            for path in sorted(boot.glob(pattern)):
                if path.is_file():
                    boot_entries[str(path.relative_to(boot))] = {
                        "size": path.stat().st_size,
                        "sha256": digest(path),
                    }
    user_files = (
        ".config/omarchy-pi-rdp",
        ".config/hypr-rdp",
        ".ssh",
    )
    user_state = {
        name: directory_record(home_path / name) for name in user_files
    }
    account_auth, account_ssh = account_records(root, home_path)
    mkinitcpio = parse_mkinitcpio_config(root)
    return {
        "created_at": int(time.time()),
        "architecture": os.environ.get("OMARCHY_PI_ARCH", platform.machine()),
        "packages": package_versions(),
        "protected": protected,
        "protected_dirs": protected_dirs,
        "boot_entries": boot_entries,
        "mkinitcpio": mkinitcpio,
        "kernel_artifacts": kernel_artifacts(root, mkinitcpio),
        "custom_recipes": recipe_hashes(source_dir),
        "user_state": user_state,
        "account_auth": account_auth,
        "account_ssh": account_ssh,
        "bootloader": bootloader_record(str(root)),
    }


def changed_protected(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for key in ("protected", "protected_dirs", "user_state"):
        old = before.get(key, {})
        new = after.get(key, {})
        for path in sorted(set(old) | set(new)):
            if old.get(path) != new.get(path):
                changed.append(path)
    for key in ("account_auth", "account_ssh"):
        old = before.get(key, {})
        new = after.get(key, {})
        for account in sorted(set(old) | set(new)):
            if old.get(account) != new.get(account):
                changed.append(f"{key}:{account}")
    if before.get("bootloader") != after.get("bootloader"):
        changed.append("bootloader")
    return changed


def compare_snapshots(before: dict[str, Any], after: dict[str, Any], *, source_dir: str | Path | None = None) -> list[str]:
    """Return protected changes, including reviewed custom-recipe drift."""

    changed = changed_protected(before, after)
    old_recipes = before.get("custom_recipes", {})
    new_recipes = after.get("custom_recipes", {})
    if old_recipes and new_recipes:
        for package in sorted(set(old_recipes) | set(new_recipes)):
            if old_recipes.get(package) != new_recipes.get(package):
                changed.append(f"custom recipe changed: {package}")
    return changed


def verify_boot_state(before: dict[str, Any], after: dict[str, Any], *, root: str | Path = "/") -> list[str]:
    failures: list[str] = []
    before_entries = before.get("boot_entries", {})
    after_entries = after.get("boot_entries", {})
    for name in sorted(before_entries):
        if name not in after_entries:
            failures.append(f"boot entry disappeared: /boot/{name}")
        elif after_entries[name].get("size", 0) <= 0:
            failures.append(f"boot entry is empty: /boot/{name}")
    if before_entries and not after_entries:
        failures.append("/boot no longer contains kernel or initramfs entries")
    if "kernel8.img" in before_entries and "kernel8.img" not in after_entries:
        failures.append("Raspberry Pi kernel8.img disappeared")
    if str(root) == "/" and is_raspberry_pi(root) and "kernel8.img" not in after_entries:
        failures.append("live Raspberry Pi is missing /boot/kernel8.img")
    old_mkinit = before.get("mkinitcpio", {})
    new_mkinit = after.get("mkinitcpio", {})
    if not set(old_mkinit.get("modules", [])) >= EXPECTED_MODULES:
        failures.append("pre-update mkinitcpio MODULES do not include the reviewed Pi storage/input set")
    if not set(old_mkinit.get("hooks", [])) >= EXPECTED_HOOKS:
        failures.append("pre-update mkinitcpio HOOKS do not include the reviewed Pi boot set")
    if old_mkinit.get("modules") != new_mkinit.get("modules"):
        failures.append("mkinitcpio MODULES changed during update")
    if old_mkinit.get("hooks") != new_mkinit.get("hooks"):
        failures.append("mkinitcpio HOOKS changed during update")
    for preset in old_mkinit.get("presets", {}):
        if preset not in new_mkinit.get("presets", {}):
            failures.append(f"mkinitcpio preset disappeared: {preset}")
    for preset, values in new_mkinit.get("presets", {}).items():
        for key, image in values.items():
            if not key.endswith("_image"):
                continue
            image_path = root_path(root, image if image.startswith("/") else "/boot/" + Path(image).name)
            if not image_path.is_file() or image_path.stat().st_size <= 0:
                failures.append(f"mkinitcpio preset {preset} points to missing image {image}")
    boot = root_path(root, "/boot")
    if not boot.is_dir():
        failures.append("/boot is not a directory")
    failures.extend(verify_kernel_artifacts(after, root=root))
    return failures


def verify_boot(before: dict[str, Any], after: dict[str, Any], *, root: str | Path = "/") -> list[str]:
    """Backward-compatible alias used by the first draft of the runner."""

    return verify_boot_state(before, after, root=root)


def binary_abi_failures(binary: Path) -> list[str]:
    failures: list[str] = []
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return [f"custom executable is missing or not executable: {binary}"]
    file_status, file_output = command_output(["file", "-Lb", str(binary)])
    if file_status != 0 or ("ELF" not in file_output and os.environ.get("OMARCHY_PI_ALLOW_TEST_BINARY") != "1"):
        failures.append(f"custom executable is not a verified ELF binary: {binary}")
    if "ELF" in file_output and "AArch64" not in file_output and "aarch64" not in file_output.lower():
        failures.append(f"custom executable is not an AArch64 binary: {binary}")
    readelf_status, readelf_output = command_output(["readelf", "-l", str(binary)])
    if readelf_status == 0:
        for line in readelf_output.splitlines():
            if "Requesting program interpreter" in line:
                interpreter = line.split(":", 1)[-1].strip().strip("[]")
                if interpreter and not Path(interpreter).is_file():
                    failures.append(f"custom executable loader is missing: {interpreter}")
    ldd_status, ldd_output = command_output(["ldd", str(binary)])
    if "not found" in ldd_output:
        failures.append(f"custom executable has unresolved shared libraries: {binary}")
    elif ldd_status not in (0, 1) and "not a dynamic executable" not in ldd_output:
        failures.append(f"could not inspect custom executable ABI: {binary}")
    return failures


def verify_custom_packages(
    before: dict[str, Any], *, root: str | Path = "/", source_dir: str | Path | None = None
) -> list[str]:
    failures: list[str] = []
    try:
        after_packages = package_versions()
    except UpdateCheckError as exc:
        return [str(exc)]
    root = Path(root)
    for package in CUSTOM_PACKAGES:
        old = before.get("packages", {}).get(package)
        if old is None:
            continue
        new = after_packages.get(package)
        if new is None:
            failures.append(f"installed custom package disappeared: {package}")
            continue
        if package == "hypr-rdp":
            binary = root_path(root, "/usr/bin/hypr-rdp")
            marker = root_path(root, "/usr/share/omarchy-pi/hypr-rdp.sha256")
            failures.extend(binary_abi_failures(binary))
            if marker.is_file() and marker.read_text().strip() != digest(binary):
                failures.append("hypr-rdp executable failed its package digest check")
        elif package == "ttfx":
            binary = root_path(root, "/usr/bin/ttfx")
            failures.extend(binary_abi_failures(binary))
    if source_dir is not None and before.get("custom_recipes"):
        old_recipes = before.get("custom_recipes", {})
        new_recipes = recipe_hashes(source_dir)
        for package in sorted(set(old_recipes) | set(new_recipes)):
            if old_recipes.get(package) != new_recipes.get(package):
                failures.append(f"custom package recipe changed without review: {package}")
    return failures


snapshot_target = snapshot
