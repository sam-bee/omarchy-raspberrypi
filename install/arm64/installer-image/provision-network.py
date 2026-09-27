#!/usr/bin/python3
"""Provision NetworkManager profiles for the isolated installer image.

The command-line entry point always operates on the running image root (``/``)
and is intended to run before NetworkManager starts.  The Python API accepts a
root tree and a command runner for unprivileged tests; tests therefore never
touch the active Pi, its network, or a physical device.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import stat
import subprocess
import tempfile
from typing import Any, Callable
import uuid

from settings import SettingsError, WifiSettings, load_settings


BUILDER_MARKER = Path("/usr/lib/omarchy-pi/installer-image.marker")
BUILDER_MARKER_CONTENT = b"omarchy-pi-installer-image-v1\n"
SETTINGS_FILE = Path("/boot/installer-settings.toml")
CONNECTION_DIRECTORY = Path("/etc/NetworkManager/system-connections")
ETHERNET_PROFILE = CONNECTION_DIRECTORY / "10-omarchy-installer-ethernet.nmconnection"
WIFI_PROFILE = CONNECTION_DIRECTORY / "20-omarchy-installer-wifi.nmconnection"
ETHERNET_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://omarchy.org/pi-installer/ethernet"))
WIFI_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://omarchy.org/pi-installer/wifi"))


class NetworkProvisionError(RuntimeError):
    """Raised when installer NetworkManager state cannot be made safely."""


@dataclass(frozen=True, slots=True)
class NetworkProvisionResult:
    ethernet_profile_written: bool
    wifi_profile_written: bool
    country_applied: bool


Runner = Callable[..., Any]


def _rooted(root: Path, absolute_path: Path) -> Path:
    if not absolute_path.is_absolute():
        raise NetworkProvisionError("internal path must be absolute")
    return root / absolute_path.relative_to("/")


def _lstat(path: Path, *, description: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError:
        raise NetworkProvisionError(f"{description} is unavailable") from None


def _validate_marker(path: Path, *, owner_uid: int) -> None:
    info = _lstat(path, description="installer image marker")
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022:
        raise NetworkProvisionError("installer image marker has unsafe type, ownership, or permissions")
    if info.st_size > 128:
        raise NetworkProvisionError("installer image marker is too large")
    try:
        content = path.read_bytes()
    except OSError:
        raise NetworkProvisionError("installer image marker is unreadable") from None
    if content != BUILDER_MARKER_CONTENT:
        raise NetworkProvisionError("installer image marker is invalid")


def _ensure_directory(path: Path, *, description: str, owner_uid: int, owner_gid: int, mode: int) -> None:
    if os.path.lexists(path):
        info = _lstat(path, description=description)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022:
            raise NetworkProvisionError(f"{description} has unsafe type, ownership, or permissions")
        return
    try:
        path.mkdir(mode=mode, parents=True, exist_ok=False)
        os.chown(path, owner_uid, owner_gid)
        os.chmod(path, mode)
    except OSError:
        raise NetworkProvisionError(f"cannot create secure {description}") from None


def _atomic_write(path: Path, payload: bytes, *, owner_uid: int, owner_gid: int, description: str) -> None:
    if os.path.lexists(path):
        info = _lstat(path, description=description)
        if stat.S_ISLNK(info.st_mode):
            raise NetworkProvisionError(f"refusing to replace symlink at {description}")
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o077:
            raise NetworkProvisionError(f"{description} has unsafe type, ownership, or permissions")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise NetworkProvisionError(f"parent directory for {description} is unsafe")
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".omarchy-pi.", dir=parent)
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, owner_uid, owner_gid)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        os.chmod(path, 0o600)
    except OSError:
        raise NetworkProvisionError(f"cannot write {description}") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _remove_profile(path: Path, *, owner_uid: int) -> None:
    if not os.path.lexists(path):
        return
    info = _lstat(path, description="installer NetworkManager profile")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o077:
        raise NetworkProvisionError("installer NetworkManager profile has unsafe type, ownership, or permissions")
    try:
        path.unlink()
    except OSError:
        raise NetworkProvisionError("cannot remove stale installer NetworkManager profile") from None


def _keyfile_value(value: str) -> str:
    """Escape a validated TOML string for a NetworkManager keyfile value."""

    # NetworkManager uses GLib's GKeyFile syntax.  A leading literal space is
    # otherwise discarded while parsing; ``\\s`` is GLib's lossless spelling.
    # Backslashes must be escaped as well.  Semicolons, '#', and '=' are valid
    # in a scalar value and must remain literal: GLib does not accept the
    # tempting ``\\;``, ``\\#``, or ``\\=`` spellings for string values.
    leading_spaces = len(value) - len(value.lstrip(" "))
    escaped = value.replace("\\", "\\\\")
    return "\\s" * leading_spaces + escaped[leading_spaces:]


def ethernet_keyfile() -> bytes:
    """Return an interface-agnostic DHCP profile for wired fallback."""

    return (
        "[connection]\n"
        "id=Omarchy Pi Installer Ethernet\n"
        f"uuid={ETHERNET_UUID}\n"
        "type=ethernet\n"
        "autoconnect=true\n"
        "\n"
        "[ipv4]\n"
        "method=auto\n"
        "\n"
        "[ipv6]\n"
        "method=auto\n"
    ).encode("utf-8")


def wifi_keyfile(wifi: WifiSettings) -> bytes:
    """Return a WPA-PSK auto-DHCP profile without naming a Wi-Fi interface."""

    ssid = _keyfile_value(wifi.ssid)
    password = _keyfile_value(wifi.password)
    return (
        "[connection]\n"
        "id=Omarchy Pi Installer Wi-Fi\n"
        f"uuid={WIFI_UUID}\n"
        "type=wifi\n"
        "autoconnect=true\n"
        "\n"
        "[wifi]\n"
        "mode=infrastructure\n"
        "security=802-11-wireless-security\n"
        f"ssid={ssid}\n"
        "\n"
        "[wifi-security]\n"
        "key-mgmt=wpa-psk\n"
        f"psk={password}\n"
        "\n"
        "[ipv4]\n"
        "method=auto\n"
        "\n"
        "[ipv6]\n"
        "method=auto\n"
    ).encode("utf-8")


def _run_country(runner: Runner, country: str) -> bool:
    """Apply the global regulatory domain when ``iw`` and the driver allow it."""

    try:
        result = runner(
            ["iw", "reg", "set", country],
            input=None,
            text=True,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(result, "returncode", 1) == 0


def provision(
    root: Path = Path("/"),
    *,
    runner: Runner = subprocess.run,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> NetworkProvisionResult:
    """Regenerate the installer profiles from the current FAT settings."""

    root = Path(root)
    _validate_marker(_rooted(root, BUILDER_MARKER), owner_uid=owner_uid)
    settings_path = _rooted(root, SETTINGS_FILE)
    try:
        settings = load_settings(settings_path)
    except SettingsError:
        raise NetworkProvisionError("installer settings are missing or invalid") from None

    connection_directory = _rooted(root, CONNECTION_DIRECTORY)
    _ensure_directory(
        connection_directory,
        description="NetworkManager system-connections directory",
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        mode=0o755,
    )
    _atomic_write(
        _rooted(root, ETHERNET_PROFILE),
        ethernet_keyfile(),
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        description="installer Ethernet NetworkManager profile",
    )

    wifi_written = False
    country_applied = False
    if settings.wifi is None:
        _remove_profile(_rooted(root, WIFI_PROFILE), owner_uid=owner_uid)
    else:
        _atomic_write(
            _rooted(root, WIFI_PROFILE),
            wifi_keyfile(settings.wifi),
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            description="installer Wi-Fi NetworkManager profile",
        )
        wifi_written = True
        country_applied = _run_country(runner, settings.wifi.country)

    return NetworkProvisionResult(
        ethernet_profile_written=True,
        wifi_profile_written=wifi_written,
        country_applied=country_applied,
    )


def main() -> int:
    if os.geteuid() != 0 or os.getuid() != 0:
        print("provision-network: must run as root", file=os.sys.stderr)
        return 2
    try:
        result = provision()
    except (OSError, NetworkProvisionError):
        print("provision-network: network provisioning failed", file=os.sys.stderr)
        return 1
    print(
        "provision-network: Ethernet profile ready; "
        + ("Wi-Fi profile ready" if result.wifi_profile_written else "Ethernet-only mode")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
