#!/usr/bin/python3
"""Small, read-only helpers shared by the Raspberry Pi updater.

The updater deliberately records facts instead of trying to make a system look
like a new installation.  In particular, the protected-state snapshot is a
comparison aid; it never writes boot files, credentials, EEPROM or network
configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import re
import subprocess
import time
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
)
PROTECTED_DIRS = (
    "/etc/mkinitcpio.conf.d",
    "/etc/NetworkManager/system-connections",
    "/etc/wpa_supplicant",
    "/etc/ssh",
)
BOOT_KERNEL_GLOBS = ("kernel8.img", "kernel_2712.img", "vmlinuz*", "initramfs*", "*.dtb")
CUSTOM_PACKAGES = ("hypr-rdp", "ttfx")
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
    """Read EEPROM state through vcgencmd only; never invoke an update utility."""

    if str(root) != "/":
        return {"method": "fixture", "output": None}
    command = shutil.which("vcgencmd")
    if command is None:
        if is_raspberry_pi(root):
            raise UpdateCheckError("vcgencmd is required for read-only Raspberry Pi EEPROM verification")
        return {"method": None, "output": None}
    status, output = command_output([command, "bootloader_config"], timeout=10)
    if status != 0 or not output.strip():
        raise UpdateCheckError("read-only vcgencmd bootloader_config failed")
    return {"method": "vcgencmd bootloader_config", "output": output}


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
            images = dict(re.findall(r"(?m)^\s*([A-Za-z0-9_]+_image)\s*=\s*['\"]([^'\"]+)", preset_text))
            if images:
                presets[str(path.relative_to(root_path(root, "/")))] = images
    return {"modules": sorted(values["MODULES"]), "hooks": sorted(values["HOOKS"]), "presets": presets}


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
    return {
        "created_at": int(time.time()),
        "architecture": os.environ.get("OMARCHY_PI_ARCH", platform.machine()),
        "packages": package_versions(),
        "protected": protected,
        "protected_dirs": protected_dirs,
        "boot_entries": boot_entries,
        "mkinitcpio": parse_mkinitcpio_config(root),
        "custom_recipes": recipe_hashes(source_dir),
        "user_state": user_state,
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
            image_path = root_path(root, image if image.startswith("/") else "/boot/" + Path(image).name)
            if not image_path.is_file() or image_path.stat().st_size <= 0:
                failures.append(f"mkinitcpio preset {preset} points to missing image {image}")
    boot = root_path(root, "/boot")
    if not boot.is_dir():
        failures.append("/boot is not a directory")
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
