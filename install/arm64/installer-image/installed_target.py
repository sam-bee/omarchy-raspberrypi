#!/usr/bin/python3
"""Provision one already-mounted Omarchy Pi target.

The disk installer owns partitioning, filesystems, mounts, and (when used)
the temporary LUKS mapper.  This module only consumes those mounted paths.  It
does not inspect or write block devices and it never connects to a Pi.

The generic desktop payload is intentionally provisioned by the reviewed
``provision-desktop-root.sh`` leaf.  Commands which must execute against the
new system run in a native, non-booting ``systemd-nspawn`` context.  The
context joins the installer's existing network namespace for setup downloads;
it does not start a target service or change the installer's NetworkManager
state.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import tempfile
from typing import Any, Callable, Mapping, Sequence
import uuid


class TargetProvisionError(RuntimeError):
    """Raised when a mounted target cannot be provisioned safely."""


Runner = Callable[..., Any]
Progress = Callable[[str], None]


_USERNAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
_HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_COUNTRY = re.compile(r"[A-Z]{2}\Z")
_UUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\Z")
_TIMEZONE = re.compile(r"[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*\Z")
_LOCALE = re.compile(r"[A-Za-z0-9_.@+-]{1,80}\Z")
_KEYMAP = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+/@-]{0,79}\Z")
_FAT_UUID = re.compile(r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}\Z")
_SOURCE_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SSH_TYPES = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }
)
_ROOT_KEYS = frozenset(
    {
        "username",
        "hostname",
        "password",
        "timezone",
        "locale",
        "keymap",
        "wifi",
        "ssh_enabled",
        "ssh_authorized_key",
        "rdp_mode",
        "rdp_password",
        "encryption",
        "recovery_passphrase",
    }
)
_WIFI_KEYS = frozenset({"country", "ssid", "password"})
_STORAGE_KEYS = frozenset({"root", "boot", "root_uuid", "boot_uuid", "luks_uuid", "key_uuid", "key_path"})
_PROVENANCE_KEYS = frozenset({"installer", "desktop_bundle_sha256"})
_INSTALLER_PROVENANCE_KEYS = frozenset({"source_revision", "runtime_sha256"})
_GENERIC_ACCOUNTS = frozenset({"alarm", "installer", "omarchy-installer", "omarchy"})
_INSTALLER_MARKERS = (
    Path("boot/installer-settings.toml"),
    Path("usr/lib/omarchy-pi/installer-image.marker"),
    Path("var/lib/omarchy-pi/access-provisioned"),
    Path("var/lib/omarchy-pi/rdp-provisioned"),
)
_INSTALLER_UNITS = frozenset(
    {
        "omarchy-installer-launch.service",
        "omarchy-installer-rdp.service",
        "omarchy-pi-provision-access.service",
        "omarchy-pi-provision-network.service",
        "omarchy-pi-provision-rdp.service",
    }
)


@dataclass(frozen=True, slots=True)
class Account:
    username: str
    uid: int
    gid: int
    home: Path


def _error(message: str) -> TargetProvisionError:
    # Error text is deliberately made from field names and fixed reasons.  A
    # caller can safely display it in the installer's progress log.
    return TargetProvisionError(message)


def _text(value: Any, field: str, *, minimum: int = 1, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise _error(f"{field} must be a string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise _error(f"{field} is not valid UTF-8") from None
    if not minimum <= size <= maximum:
        raise _error(f"{field} has an invalid length")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise _error(f"{field} contains a control character")
    return value


def _password(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise _error(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise _error(f"{field} must be a string")
    if value == "":
        if required:
            raise _error(f"{field} is required")
        return None
    return _text(value, field, minimum=8, maximum=256)


def _authorized_key(value: Any) -> str | None:
    if value is None or value == "":
        return None
    value = _text(value, "ssh_authorized_key", maximum=8192)
    parts = value.split(" ")
    if len(parts) < 2 or parts[0] not in _SSH_TYPES or not parts[1]:
        raise _error("ssh_authorized_key is not one OpenSSH public key")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (ValueError, binascii.Error):
        raise _error("ssh_authorized_key has invalid encoding") from None
    if len(blob) < 32:
        raise _error("ssh_authorized_key has an invalid payload")
    if any(part == "" for part in parts):
        raise _error("ssh_authorized_key has invalid spacing")
    return value


def validate_settings(settings: dict) -> dict:
    """Validate installer input and return a secret-bearing private copy.

    The returned object is for the worker only.  It must never be serialized
    into a job record or passed to ``progress``.  Validation is independent of
    the target filesystem; timezone, locale, and keymap existence is checked
    again against the mounted target before any target mutation.
    """

    if not isinstance(settings, dict):
        raise _error("settings must be a mapping")
    unknown = set(settings) - _ROOT_KEYS
    if unknown:
        raise _error("settings contains an unknown field")
    if set(settings) != _ROOT_KEYS:
        raise _error("settings is missing a required field")

    username = _text(settings.get("username"), "username", maximum=32)
    if not _USERNAME.fullmatch(username) or username in {"root", "nobody", *_GENERIC_ACCOUNTS}:
        raise _error("username has an invalid name")
    hostname = _text(settings.get("hostname"), "hostname", maximum=63)
    if not _HOSTNAME.fullmatch(hostname):
        raise _error("hostname has an invalid name")
    timezone = _text(settings.get("timezone"), "timezone", maximum=128)
    if not _TIMEZONE.fullmatch(timezone) or timezone.startswith("/") or ".." in timezone.split("/"):
        raise _error("timezone has an invalid name")
    locale = _text(settings.get("locale"), "locale", maximum=80)
    if not _LOCALE.fullmatch(locale):
        raise _error("locale has an invalid name")
    keymap = _text(settings.get("keymap"), "keymap", maximum=80)
    if not _KEYMAP.fullmatch(keymap) or ".." in keymap.split("/"):
        raise _error("keymap has an invalid name")

    wifi_value = settings.get("wifi")
    wifi: dict[str, str] | None
    if wifi_value is None:
        wifi = None
    elif isinstance(wifi_value, dict):
        if set(wifi_value) != _WIFI_KEYS:
            raise _error("wifi contains an unknown or missing field")
        country = _text(wifi_value["country"], "wifi.country", maximum=2)
        if not _COUNTRY.fullmatch(country):
            raise _error("wifi.country must be an uppercase country code")
        ssid = _text(wifi_value["ssid"], "wifi.ssid", maximum=32)
        password = _text(wifi_value["password"], "wifi.password", maximum=64)
        printable = 8 <= len(password.encode("utf-8")) <= 63 and all(0x20 <= ord(char) <= 0x7E for char in password)
        if not printable and not re.fullmatch(r"[0-9A-Fa-f]{64}", password):
            raise _error("wifi.password has an invalid WPA-PSK form")
        wifi = {"country": country, "ssid": ssid, "password": password}
    else:
        raise _error("wifi must be a mapping or null")

    ssh_enabled = settings.get("ssh_enabled")
    if not isinstance(ssh_enabled, bool):
        raise _error("ssh_enabled must be a boolean")
    ssh_key = _authorized_key(settings.get("ssh_authorized_key"))
    rdp_mode = settings.get("rdp_mode")
    if rdp_mode not in {"disabled", "loopback", "lan"}:
        raise _error("rdp_mode is invalid")
    rdp_password = _password(settings.get("rdp_password"), "rdp_password", required=rdp_mode != "disabled")
    if rdp_mode == "disabled" and settings.get("rdp_password") not in {None, ""}:
        raise _error("rdp_password must be empty when RDP is disabled")

    encryption = settings.get("encryption")
    if encryption not in {"plain", "passphrase", "key"}:
        raise _error("encryption is invalid")
    recovery = _password(settings.get("recovery_passphrase"), "recovery_passphrase", required=encryption != "plain")
    if encryption == "plain" and settings.get("recovery_passphrase") not in {None, ""}:
        raise _error("recovery_passphrase must be empty for plain root")

    return {
        "username": username,
        "hostname": hostname,
        "password": _password(settings.get("password"), "password"),
        "timezone": timezone,
        "locale": locale,
        "keymap": keymap,
        "wifi": wifi,
        "ssh_enabled": ssh_enabled,
        "ssh_authorized_key": ssh_key,
        "rdp_mode": rdp_mode,
        "rdp_password": rdp_password,
        "encryption": encryption,
        "recovery_passphrase": recovery,
    }


def _validate_provenance(provenance: Any) -> dict[str, Any] | None:
    """Validate the fixed, non-secret installer provenance contract."""

    if provenance is None:
        return None
    if not isinstance(provenance, dict) or set(provenance) != _PROVENANCE_KEYS:
        raise _error("provenance has unknown or missing fields")
    installer = provenance["installer"]
    if not isinstance(installer, dict) or set(installer) != _INSTALLER_PROVENANCE_KEYS:
        raise _error("provenance.installer has unknown or missing fields")
    source_revision = installer["source_revision"]
    if source_revision is not None and (
        not isinstance(source_revision, str) or not _SOURCE_REVISION.fullmatch(source_revision)
    ):
        raise _error("provenance.installer.source_revision is invalid")
    runtime_sha256 = installer["runtime_sha256"]
    if not isinstance(runtime_sha256, str) or not _SHA256.fullmatch(runtime_sha256):
        raise _error("provenance.installer.runtime_sha256 is invalid")
    desktop_bundle_sha256 = provenance["desktop_bundle_sha256"]
    if not isinstance(desktop_bundle_sha256, str) or not _SHA256.fullmatch(desktop_bundle_sha256):
        raise _error("provenance.desktop_bundle_sha256 is invalid")
    return {
        "installer": {
            "source_revision": source_revision,
            "runtime_sha256": runtime_sha256,
        },
        "desktop_bundle_sha256": desktop_bundle_sha256,
    }


def _lexical_absolute(path: Path) -> Path:
    if not path.is_absolute():
        raise _error("target paths must be absolute")
    return Path(os.path.normpath(os.fspath(path)))


def _reject_symlink_components(path: Path, *, include_leaf: bool = True) -> None:
    absolute = _lexical_absolute(path)
    current = Path(absolute.anchor)
    parts = absolute.parts[1:]
    if not include_leaf:
        parts = parts[:-1]
    for part in parts:
        current /= part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise _error("target path contains a symlink component")
        except FileNotFoundError:
            break


def _validate_uuid(value: Any, field: str, *, required: bool = False, fat_serial: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise _error(f"{field} is required")
        return None
    pattern = _FAT_UUID if fat_serial else _UUID
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _error(f"{field} is not a UUID")
    if set(value.replace("-", "")) == {"0"}:
        raise _error(f"{field} is not a usable UUID")
    # FAT volume serials are emitted by blkid in the filesystem's displayed
    # case.  Preserve that spelling for the fstab entry; canonical UUIDs use
    # lower case everywhere else.
    return value if fat_serial else value.lower()


def _validate_storage(storage: Mapping[str, Any], settings: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(storage, Mapping) or set(storage) != _STORAGE_KEYS:
        raise _error("storage has unknown or missing fields")
    try:
        root = _lexical_absolute(Path(storage["root"]))
        boot = _lexical_absolute(Path(storage["boot"]))
    except (TypeError, ValueError):
        raise _error("storage paths are invalid") from None
    if root == Path("/") or boot == Path("/") or boot != root / "boot":
        raise _error("storage paths do not describe a mounted target")
    _reject_symlink_components(root)
    _reject_symlink_components(boot)
    if not root.is_dir() or not boot.is_dir():
        raise _error("mounted target paths are unavailable")
    if not (root / "etc/passwd").is_file() or not (root / "etc/group").is_file():
        raise _error("target account databases are missing")
    if not (root / "boot").is_dir():
        raise _error("target boot directory is missing")

    result = {
        "root": root,
        "boot": boot,
        "root_uuid": _validate_uuid(storage["root_uuid"], "root_uuid", required=True),
        "boot_uuid": _validate_uuid(storage["boot_uuid"], "boot_uuid", required=True, fat_serial=True),
        "luks_uuid": _validate_uuid(storage["luks_uuid"], "luks_uuid", required=settings["encryption"] != "plain"),
        "key_uuid": _validate_uuid(storage["key_uuid"], "key_uuid", required=settings["encryption"] == "key"),
        "key_path": None,
    }
    if settings["encryption"] == "plain" and any(
        storage[field] not in {None, ""} for field in ("luks_uuid", "key_uuid", "key_path")
    ):
        raise _error("plain encryption cannot carry encrypted storage metadata")
    if settings["encryption"] == "passphrase" and any(
        storage[field] not in {None, ""} for field in ("key_uuid", "key_path")
    ):
        raise _error("passphrase encryption cannot carry key storage metadata")
    key_path = storage["key_path"]
    if settings["encryption"] == "key":
        # disk_install deliberately returns the path as seen inside the
        # disposable key filesystem.  It is metadata for cmdline generation,
        # never a host path: the key bytes stay on that filesystem and are
        # never read or copied by target provisioning.
        try:
            public_key_path = os.fspath(key_path)
        except TypeError:
            raise _error("key_path must be the target key-filesystem path") from None
        if public_key_path != "/.cryptroot.key":
            raise _error("key_path must be the target key-filesystem path")
        key_path = "/.cryptroot.key"
    elif key_path is not None and key_path != "":
        raise _error("key_path is only valid for key encryption")
    else:
        key_path = None
    result["key_path"] = key_path
    return result


def _require_directory(path: Path, description: str) -> None:
    _reject_symlink_components(path)
    if not path.is_dir() or path.is_symlink():
        raise _error(f"{description} is unavailable")


def _target_path(root: Path, relative: str) -> Path:
    if not relative.startswith("/"):
        raise _error("internal target path is not absolute")
    path = Path(os.path.normpath(os.fspath(root / relative.lstrip("/"))))
    if path != root and not path.is_relative_to(root):
        raise _error("internal target path escapes the target")
    _reject_symlink_components(path, include_leaf=False)
    return path


def _ensure_parent(path: Path) -> None:
    parent = path.parent
    _reject_symlink_components(parent)
    parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise _error("target parent is unsafe")


def _ensure_public_directory(path: Path, description: str) -> None:
    """Create the root-owned public directory used by a target service file."""

    _reject_symlink_components(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o755, parents=True, exist_ok=False)
            info = path.lstat()
        except OSError:
            raise _error(f"could not create {description}") from None
    except OSError:
        raise _error(f"could not inspect {description}") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _error(f"{description} is not a real directory")
    if os.geteuid() == 0 and (info.st_uid != 0 or info.st_gid != 0):
        raise _error(f"{description} has the wrong owner")
    try:
        os.chmod(path, 0o755)
        info = path.lstat()
    except OSError:
        raise _error(f"could not set {description} permissions") from None
    if stat.S_IMODE(info.st_mode) != 0o755 or (os.geteuid() == 0 and (info.st_uid != 0 or info.st_gid != 0)):
        raise _error(f"{description} has unsafe permissions")


def _atomic_write(
    path: Path,
    data: bytes,
    *,
    mode: int,
    uid: int = 0,
    gid: int = 0,
    chown: bool = True,
) -> None:
    # The real worker is root-owned.  Keeping fixture writes usable by an
    # unprivileged unit-test process lets the command-runner tests exercise
    # path and secret boundaries without sudo or a user namespace.
    if chown and os.geteuid() != 0 and uid == 0 and gid == 0:
        uid, gid = os.getuid(), os.getgid()
    _ensure_parent(path)
    if os.path.lexists(path):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise _error("refusing to replace an unsafe target file")
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".omarchy-target.", dir=path.parent)
        temporary = Path(temporary_name)
        os.fchmod(descriptor, mode)
        if chown:
            os.fchown(descriptor, uid, gid)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        os.chmod(path, mode)
        if chown:
            os.chown(path, uid, gid)
    except OSError:
        raise _error("could not write target configuration") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _persist_provenance(root: Path, provenance: Mapping[str, Any]) -> None:
    """Extend the desktop leaf's existing provenance record after validation."""

    path = _target_path(root, "/var/lib/omarchy-pi/desktop-user-provision.json")
    if not path.is_file() or path.is_symlink():
        raise _error("desktop provenance record is missing")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise _error("desktop provenance record is unreadable") from None
    if not isinstance(record, dict):
        raise _error("desktop provenance record is invalid")
    source_revision = record.get("source_revision")
    if not isinstance(source_revision, str) or not _SOURCE_REVISION.fullmatch(source_revision):
        raise _error("desktop provenance source revision is invalid")
    record["installer"] = {
        "source_revision": provenance["installer"]["source_revision"],
        "runtime_sha256": provenance["installer"]["runtime_sha256"],
    }
    record["desktop_bundle_sha256"] = provenance["desktop_bundle_sha256"]
    try:
        encoded = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    except (TypeError, ValueError):
        raise _error("desktop provenance record cannot be serialized") from None
    _atomic_write(path, encoded, mode=0o644)


def _remove_managed(path: Path) -> None:
    if not os.path.lexists(path):
        return
    _reject_symlink_components(path, include_leaf=False)
    info = path.lstat()
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise _error("refusing to remove an unsafe target path")
    if not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o002:
        raise _error("refusing to remove a world-writable target path")
    if stat.S_ISDIR(info.st_mode):
        # Managed directories are only removed when empty.  User data is never
        # recursively deleted as part of a target install.
        try:
            path.rmdir()
        except OSError:
            raise _error("managed target directory is not empty") from None
    else:
        path.unlink()


def _run(
    runner: Runner,
    command: Sequence[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    capture_output: bool = True,
) -> Any:
    try:
        result = runner(
            list(command),
            input=input_text,
            text=True,
            capture_output=capture_output,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise _error("target command could not be started") from None
    if check and getattr(result, "returncode", 1) != 0:
        raise _error("target command failed")
    return result


def _nspawn_prefix(root: Path, boot: Path, user: str | None = None) -> list[str]:
    prefix = [
        "systemd-nspawn",
        "--quiet",
        "--register=no",
        "--private-users=no",
        "--network-namespace-path=/proc/1/ns/net",
        "--resolv-conf=replace-host",
        # nspawn defaults to --timezone=auto, which can replace or remove the
        # mounted target's /etc/localtime using the installer's host timezone.
        # Regional settings belong to the target and must survive every
        # isolated setup command.
        "--timezone=off",
        # Keep stdin a pipe so commands such as chpasswd receive EOF after
        # the supplied secret.  Without this, nspawn allocates a pty and the
        # child can remain blocked waiting for interactive input.
        "--pipe",
        "--bind=" + os.fspath(boot) + ":/boot",
        "--directory",
        os.fspath(root),
    ]
    if user is not None:
        account_home = "/home/" + user
        prefix.extend(["--setenv=HOME=" + account_home, "--setenv=USER=" + user, "--setenv=LOGNAME=" + user])
        prefix.append("--user=" + user)
    prefix.append("--")
    return prefix


def _target_exec(
    root: Path,
    boot: Path,
    command: Sequence[str],
    *,
    runner: Runner,
    input_text: str | None = None,
    check: bool = True,
    user: str | None = None,
) -> Any:
    return _run(runner, [*_nspawn_prefix(root, boot, user), *command], input_text=input_text, check=check)


def _load_boot_helper() -> Any:
    path = Path(__file__).with_name("configure-installer-boot.py")
    if not path.is_file():
        raise _error("Pi boot helper is missing")
    spec = importlib.util.spec_from_file_location("omarchy_target_boot", path)
    if spec is None or spec.loader is None:
        raise _error("Pi boot helper cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    # dataclasses with postponed annotations inspect sys.modules while the
    # module is being executed.  Register the dynamically loaded helper just
    # as importlib's normal machinery would do.
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _source_and_script(payload: Path, root: Path) -> tuple[Path, Path, Path]:
    payload = _lexical_absolute(payload)
    _reject_symlink_components(payload)
    if not payload.is_dir() or payload.is_symlink():
        raise _error("desktop payload is unavailable")
    source = payload / "source"
    if not source.is_dir():
        if (payload / ".git").is_dir():
            source = payload
        else:
            raise _error("desktop payload source checkout is missing")
    _reject_symlink_components(source)
    script_candidates = (
        source / "install/arm64/provision-desktop-root.sh",
        Path(__file__).resolve().parents[1] / "provision-desktop-root.sh",
        root / "usr/share/omarchy-pi/install/arm64/provision-desktop-root.sh",
    )
    script = next((candidate for candidate in script_candidates if candidate.is_file() and not candidate.is_symlink()), None)
    if script is None:
        raise _error("desktop root provisioner is missing")
    return source, payload, script


def _payload_runtime_layout(payload: Path, source: Path) -> str:
    """Select the target layout from the already verified desktop manifest."""

    manifest_path = payload / "desktop-manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return "legacy"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise _error("desktop manifest is unreadable") from None
    runtime = manifest.get("runtime") if isinstance(manifest, dict) else None
    if runtime is None:
        return "legacy"
    if not isinstance(runtime, Mapping) or runtime.get("layout") != "packaged" or runtime.get("path") != "/usr/share/omarchy":
        raise _error("desktop runtime layout is invalid")
    revision = runtime.get("source_revision")
    if not isinstance(revision, str) or not _SOURCE_REVISION.fullmatch(revision):
        raise _error("desktop runtime source revision is invalid")
    try:
        actual = subprocess.run(
            ["git", "-C", os.fspath(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        raise _error("desktop runtime source revision cannot be verified") from None
    if actual != revision:
        raise _error("desktop runtime source revision differs from the payload source")
    return "packaged"


def _account_from_target(root: Path, username: str) -> Account:
    try:
        lines = (root / "etc/passwd").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        raise _error("target passwd database is unreadable") from None
    for line in lines:
        fields = line.split(":")
        if len(fields) >= 7 and fields[0] == username:
            try:
                uid, gid = int(fields[2]), int(fields[3])
            except ValueError:
                raise _error("target account has invalid IDs") from None
            home = fields[5]
            if uid <= 0 or not home.startswith("/") or home == "/":
                raise _error("target account has an invalid home")
            home_path = _target_path(root, home)
            if not home_path.is_dir() or home_path.is_symlink():
                raise _error("target account home is unavailable")
            return Account(username, uid, gid, home_path)
    raise _error("target account was not created")


def _account_already_exists(root: Path, username: str) -> bool:
    try:
        return any(line.startswith(username + ":") for line in (root / "etc/passwd").read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        raise _error("target passwd database is unreadable") from None


def validate_target_options(root: Path, settings: dict) -> dict:
    """Validate regional options against a prepared generic target root.

    This is a read-only preflight intended for the worker before it erases a
    selected disk.  It deliberately uses only files already present in the
    verified generic payload: no nspawn, locale generation, keymap loading,
    network setup, or target service is involved.  The returned mapping is the
    same private validated settings copy used by :func:`provision_target`.
    """

    validated = validate_settings(settings)
    target = _lexical_absolute(Path(root))
    if target == Path("/"):
        raise _error("refusing the host root")
    _reject_symlink_components(target)
    if not target.is_dir() or target.is_symlink():
        raise _error("target root is unavailable")

    timezone = _target_path(target, "/usr/share/zoneinfo/" + validated["timezone"])
    if not timezone.is_file() or timezone.is_symlink():
        raise _error("timezone is not available in the target")

    locale = validated["locale"]
    locale_base = locale.split(".", 1)[0].split("@", 1)[0]
    if locale.upper() not in {"C", "C.UTF-8", "POSIX"}:
        locale_source = _target_path(target, "/usr/share/i18n/locales/" + locale_base)
        if not locale_source.is_file() or locale_source.is_symlink():
            raise _error("locale source is not available in the target")

    keymap_root = _target_path(target, "/usr/share/kbd/keymaps")
    if not keymap_root.is_dir() or keymap_root.is_symlink():
        raise _error("target keymap directory is unavailable")
    matches = list(keymap_root.rglob(validated["keymap"] + ".map.gz")) + list(
        keymap_root.rglob(validated["keymap"] + ".map")
    )
    if not any(item.is_file() and not item.is_symlink() for item in matches):
        raise _error("keymap is not available in the target")
    return validated


def _validate_target_locale(root: Path, locale: str, *, runner: Runner, boot: Path) -> None:
    base = locale.split(".", 1)[0].split("@", 1)[0]
    source_available = locale.upper() in {"C", "C.UTF-8", "POSIX"} or _target_path(
        root, f"/usr/share/i18n/locales/{base}"
    ).is_file()
    if source_available:
        return
    result = _target_exec(root, boot, ["/usr/bin/locale", "-a"], runner=runner, check=False)
    output = getattr(result, "stdout", "") or ""
    if output.strip():
        wanted = locale.lower().replace("utf-8", "utf8")
        available = {line.strip().lower().replace("utf-8", "utf8") for line in output.splitlines()}
        if wanted not in available:
            raise _error("locale is not available in the target")
        return
    raise _error("locale is not available in the target")


def _validate_target_keymap(root: Path, keymap: str, *, runner: Runner, boot: Path) -> None:
    result = _target_exec(root, boot, ["/usr/bin/localectl", "list-keymaps"], runner=runner, check=False)
    output = getattr(result, "stdout", "") or ""
    if output.strip():
        if keymap not in {line.strip() for line in output.splitlines()}:
            raise _error("keymap is not available in the target")
        return
    keyboard_root = _target_path(root, "/usr/share/kbd/keymaps")
    if keyboard_root.is_dir():
        matches = list(keyboard_root.rglob(keymap + ".map.gz")) + list(keyboard_root.rglob(keymap + ".map"))
        if matches:
            return
    raise _error("keymap is not available in the target")


def _configure_identity(root: Path, settings: Mapping[str, Any]) -> None:
    _atomic_write(_target_path(root, "/etc/hostname"), (settings["hostname"] + "\n").encode(), mode=0o644)
    machine_id_path = _target_path(root, "/etc/machine-id")
    if os.path.lexists(machine_id_path):
        _remove_managed(machine_id_path)
    machine_id = uuid.uuid4().hex + "\n"
    _atomic_write(machine_id_path, machine_id.encode(), mode=0o444)


def _configure_region(root: Path, boot: Path, settings: Mapping[str, Any], *, runner: Runner) -> None:
    _validate_target_locale(root, settings["locale"], runner=runner, boot=boot)
    _validate_target_keymap(root, settings["keymap"], runner=runner, boot=boot)
    zoneinfo = _target_path(root, "/usr/share/zoneinfo/" + settings["timezone"])
    if not zoneinfo.is_file() or zoneinfo.is_symlink():
        raise _error("timezone is not available in the target")
    _atomic_write(_target_path(root, "/etc/locale.conf"), (f"LANG={settings['locale']}\n").encode(), mode=0o644)
    _atomic_write(_target_path(root, "/etc/vconsole.conf"), (f"KEYMAP={settings['keymap']}\n").encode(), mode=0o644)
    locale_gen = _target_path(root, "/etc/locale.gen")
    locale_gen_text = locale_gen.read_text(encoding="utf-8") if locale_gen.exists() else ""
    if settings["locale"].upper() not in {"C", "C.UTF-8", "POSIX"}:
        locale_line = f"{settings['locale']} UTF-8"
        locale_lines = []
        found = False
        for line in locale_gen_text.splitlines():
            stripped = line.lstrip("# ")
            if stripped.startswith(settings["locale"] + " "):
                locale_lines.append(locale_line)
                found = True
            else:
                locale_lines.append(line)
        if not found:
            locale_lines.append(locale_line)
        _atomic_write(locale_gen, ("\n".join(locale_lines).rstrip() + "\n").encode(), mode=0o644)
    localtime = _target_path(root, "/etc/localtime")
    if os.path.lexists(localtime):
        _remove_managed(localtime)
    localtime.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    localtime.symlink_to("/usr/share/zoneinfo/" + settings["timezone"])
    _target_exec(root, boot, ["/usr/bin/locale-gen"], runner=runner)


def _keyfile_value(value: str) -> str:
    leading_spaces = len(value) - len(value.lstrip(" "))
    return "\\s" * leading_spaces + value[leading_spaces:].replace("\\", "\\\\")


def _configure_network(root: Path, settings: Mapping[str, Any]) -> None:
    directory = _target_path(root, "/etc/NetworkManager/system-connections")
    _reject_symlink_components(directory)
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise _error("target NetworkManager directory is unsafe")
    ethernet = (
        "[connection]\n"
        "id=Omarchy Pi Target Ethernet\n"
        f"uuid={uuid.uuid4()}\n"
        "type=ethernet\n"
        "autoconnect=true\n\n"
        "[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n"
    )
    _atomic_write(directory / "10-omarchy-pi-target-ethernet.nmconnection", ethernet.encode(), mode=0o600)
    wifi_path = directory / "20-omarchy-pi-target-wifi.nmconnection"
    if settings["wifi"] is None:
        _remove_managed(wifi_path)
        _remove_managed(_target_path(root, "/etc/modprobe.d/omarchy-pi-regdom.conf"))
        return
    wifi = settings["wifi"]
    wifi_text = (
        "[connection]\n"
        "id=Omarchy Pi Target Wi-Fi\n"
        f"uuid={uuid.uuid4()}\n"
        "type=wifi\n"
        "autoconnect=true\n\n"
        "[wifi]\n"
        "mode=infrastructure\n"
        "security=802-11-wireless-security\n"
        f"ssid={_keyfile_value(wifi['ssid'])}\n\n"
        "[wifi-security]\n"
        "key-mgmt=wpa-psk\n"
        f"psk={_keyfile_value(wifi['password'])}\n\n"
        "[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n"
    )
    _atomic_write(wifi_path, wifi_text.encode(), mode=0o600)
    regdom = f"# Omarchy Pi target regulatory domain\noptions cfg80211 ieee80211_regdom={wifi['country']}\n"
    _atomic_write(_target_path(root, "/etc/modprobe.d/omarchy-pi-regdom.conf"), regdom.encode(), mode=0o644)


def _enable_system_unit(root: Path, unit: str, *, runner: Runner) -> None:
    _run(runner, ["systemctl", "--root", os.fspath(root), "enable", unit])


def _disable_target_ssh(root: Path) -> None:
    wants = _target_path(root, "/etc/systemd/system/multi-user.target.wants/sshd.service")
    if os.path.lexists(wants):
        if not wants.is_symlink() or os.readlink(wants) not in {"/usr/lib/systemd/system/sshd.service", "../sshd.service"}:
            raise _error("target sshd enablement is not managed")
        wants.unlink()


def _configure_ssh(root: Path, boot: Path, account: Account, settings: Mapping[str, Any], *, runner: Runner) -> None:
    ssh_directory = _target_path(root, "/etc/ssh")
    _reject_symlink_components(ssh_directory)
    ssh_directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    if ssh_directory.is_symlink() or not ssh_directory.is_dir():
        raise _error("target SSH directory is unsafe")
    for path in ssh_directory.glob("ssh_host_*_key"):
        if path.is_file() or path.is_symlink():
            path.unlink()
    for path in ssh_directory.glob("ssh_host_*_key.pub"):
        if path.is_file() or path.is_symlink():
            path.unlink()
    _target_exec(root, boot, ["/usr/bin/ssh-keygen", "-A"], runner=runner)

    if settings["ssh_enabled"]:
        _enable_system_unit(root, "sshd.service", runner=runner)
        if settings["ssh_authorized_key"] is not None:
            directory = account.home / ".ssh"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            _atomic_write(
                directory / "authorized_keys",
                (settings["ssh_authorized_key"] + "\n").encode(),
                mode=0o600,
                uid=account.uid,
                gid=account.gid,
            )
    else:
        _disable_target_ssh(root)


def _configure_admin(root: Path, account: Account, *, runner: Runner, boot: Path) -> Path:
    sudoers = _target_path(root, f"/etc/sudoers.d/90-omarchy-pi-{account.username}")
    _atomic_write(sudoers, f"{account.username} ALL=(ALL:ALL) ALL\n".encode(), mode=0o440)
    _target_exec(root, boot, ["/usr/bin/visudo", "-c", "-f", f"/etc/sudoers.d/{sudoers.name}"], runner=runner)
    return sudoers


def _runtime_validator_source() -> Path:
    """Locate the installer-staged validator, with a source-tree test fallback."""

    candidates = (
        Path(__file__).resolve().with_name("verify-hypr-rdp-runtime.py"),
        Path(__file__).resolve().parents[1] / "session/systemd/verify-hypr-rdp-runtime.py",
    )
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    raise _error("corrected target RDP runtime validator is unavailable")


def _runtime_validator_bytes() -> bytes:
    """Read the exact reviewed validator staged beside this provisioner."""

    source = _runtime_validator_source()
    try:
        return source.read_bytes()
    except OSError:
        raise _error("corrected target RDP runtime validator is unreadable") from None


def _overlay_runtime_validator(root: Path, runtime_layout: str = "legacy") -> None:
    """Check the package validator, or overlay the legacy bundled copy."""

    source_bytes = _runtime_validator_bytes()
    destination_path = (
        "/usr/libexec/omarchy-pi/verify-hypr-rdp-runtime.py"
        if runtime_layout == "packaged"
        else "/usr/local/libexec/omarchy-pi/verify-hypr-rdp-runtime.py"
    )
    destination = _target_path(root, destination_path)
    if not destination.is_file() or destination.is_symlink():
        raise _error("target RDP runtime validator is missing")
    if runtime_layout == "packaged":
        if not stat.S_IMODE(destination.stat().st_mode) & 0o111:
            raise _error("packaged RDP runtime validator is not executable")
        return
    _atomic_write(destination, source_bytes, mode=0o755)


def _configure_password(root: Path, boot: Path, username: str, password: str, *, runner: Runner) -> None:
    _target_exec(root, boot, ["/usr/bin/chpasswd"], runner=runner, input_text=f"{username}:{password}\n")


def _create_rdp_profile(
    root: Path,
    account: Account,
    settings: Mapping[str, Any],
    runtime_layout: str = "legacy",
) -> str:
    profile_root = account.home / ".config/omarchy-pi-rdp"
    tls_root = account.home / ".config/hypr-rdp"
    policy_path = _target_path(root, "/etc/omarchy-pi/rdp-profile.toml")
    mode = settings["rdp_mode"]
    wants = account.home / ".config/systemd/user/graphical-session.target.wants/omarchy-pi-hypr-rdp.service"
    rdp_unit = (
        "/usr/lib/systemd/user/omarchy-pi-hypr-rdp.service"
        if runtime_layout == "packaged"
        else "/etc/systemd/user/omarchy-pi-hypr-rdp.service"
    )
    if mode == "disabled":
        _remove_managed(profile_root / "config.toml")
        _remove_managed(profile_root / "password")
        _remove_managed(profile_root)
        _remove_managed(tls_root)
        _remove_managed(policy_path)
        if os.path.lexists(wants):
            if not wants.is_symlink() or os.readlink(wants) != rdp_unit:
                raise _error("target RDP enablement is not managed")
            wants.unlink()
        return "disabled"
    if os.path.lexists(profile_root) or os.path.lexists(tls_root) or os.path.lexists(policy_path):
        raise _error("target RDP profile already exists")
    _reject_symlink_components(profile_root.parent)
    _ensure_public_directory(policy_path.parent, "target RDP profile directory")
    profile_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chown(profile_root, account.uid, account.gid)
    os.chmod(profile_root, 0o700)
    bind = "127.0.0.1:3389" if mode == "loopback" else "0.0.0.0:3389"
    password_path = profile_root / "password"
    # The file is written through the installer's mounted host path, but the
    # target service reads its configuration after the mount prefix is gone.
    target_password_path = f"/home/{account.username}/.config/omarchy-pi-rdp/password"
    # json.dumps gives TOML a quoted path without ever putting the password in
    # the config file.  It is kept local so the password remains file-only.
    config = (
        f'bind = "{bind}"\n'
        f'username = {json.dumps(account.username)}\n'
        f"password_file = {json.dumps(target_password_path)}\n"
        'resolution = "1280x720"\n'
        "fps = 20\n"
        'egfx_codec = "avc420"\n'
        'audio_mode = "off"\n'
        'file_transfer_mode = "off"\n'
    )
    policy = f'username = {json.dumps(account.username)}\nbind = {json.dumps(bind)}\n'.encode("utf-8")
    try:
        _atomic_write(password_path, settings["rdp_password"].encode(), mode=0o600, uid=account.uid, gid=account.gid)
        _atomic_write(profile_root / "config.toml", config.encode(), mode=0o600, uid=account.uid, gid=account.gid)
        _atomic_write(policy_path, policy, mode=0o644)
        _ensure_parent(wants)
        if os.path.lexists(wants):
            if not wants.is_symlink() or os.readlink(wants) != rdp_unit:
                raise _error("target RDP enablement is not managed")
        else:
            wants.symlink_to(rdp_unit)
            os.lchown(wants, account.uid, account.gid)
    except BaseException:
        for path in (profile_root / "config.toml", password_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        for directory in (profile_root,):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            policy_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return bind


def _write_fstab(root: Path, storage: Mapping[str, Any]) -> None:
    path = _target_path(root, "/etc/fstab")
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    kept: list[str] = []
    for line in existing.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] in {"/", "/boot"}:
            continue
        kept.append(line)
    kept.extend(
        [
            f"UUID={storage['root_uuid']} / ext4 defaults 0 1",
            f"UUID={storage['boot_uuid']} /boot vfat defaults 0 2",
        ]
    )
    _atomic_write(path, ("\n".join(kept).rstrip() + "\n").encode(), mode=0o644)


def _write_cmdline(root: Path, storage: Mapping[str, Any], mode: str) -> None:
    path = _target_path(root, "/boot/cmdline.txt")
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    remove = re.compile(r"^(?:root|rootfstype|rootflags|rd\.luks(?:\.[^=]+)?|rd\.crypt(?:\.[^=]+)?|cryptdevice|cryptroot)=")
    args = [argument for argument in existing.split() if not remove.match(argument)]
    args.extend([f"root={'UUID=' + storage['root_uuid'] if mode == 'plain' else '/dev/mapper/cryptroot'}", "rootfstype=ext4", "rw"])
    if mode != "plain":
        args.append(f"rd.luks.name={storage['luks_uuid']}=cryptroot")
        if mode == "key":
            args.append(f"rd.luks.key={storage['luks_uuid']}=/.cryptroot.key:UUID={storage['key_uuid']}")
            args.append(f"rd.luks.options={storage['luks_uuid']}=keyfile-timeout=10s")
    # FAT boot files expose fixed mount ownership and may reject chown; the
    # existing boot helper follows the same metadata-preserving rule.
    _atomic_write(path, (" ".join(args) + "\n").encode(), mode=0o644, chown=False)


def _boot_runner(runner: Runner, root: Path, boot: Path) -> Runner:
    def run(command: Sequence[str], **kwargs: Any) -> Any:
        command_list = list(command)
        if command_list and command_list[0] == "systemd-nspawn":
            command_list = [item for item in command_list if item != "--private-network"]
            insert_at = command_list.index("--register=no") + 1 if "--register=no" in command_list else 1
            command_list[insert_at:insert_at] = [
                "--private-users=no",
                "--network-namespace-path=/proc/1/ns/net",
                "--resolv-conf=replace-host",
                "--timezone=off",
                "--pipe",
                "--bind=" + os.fspath(boot) + ":/boot",
            ]
        return runner(command_list, **kwargs)

    return run


def _configure_boot(root: Path, boot: Path, storage: Mapping[str, Any], *, runner: Runner, machine: str | None) -> None:
    bootmod = _load_boot_helper()
    try:
        checked_root = bootmod.validate_target_root(root)
        kernel = bootmod._select_kernel(boot, None)
        bootmod._validate_linux_rpi_provenance(checked_root, kernel)
        bootmod._validate_linux_rpi_preset(checked_root)
        dtb = bootmod._select_dtb(boot)
        overlay = bootmod._regular_file(boot / bootmod.OVERLAY_FILE, description="Pi 5 DRM overlay")
        bootmod._regular_file(checked_root / bootmod.MKINITCPIO_CONFIG, description="target mkinitcpio.conf")
        bootmod._regular_file(checked_root / bootmod.INITRAMFS_FILE, description="target initramfs")
        config = bootmod._optional_text(checked_root / bootmod.CONFIG_FILE, description="target config.txt")
        if "\x00" in config:
            raise _error("target config.txt contains a NUL byte")
        bootmod._atomic_write(
            checked_root / bootmod.CONFIG_FILE,
            bootmod.build_config(config, kernel=kernel.name).encode(),
            mode=0o644,
        )
        encrypted = storage["luks_uuid"] is not None
        hooks = "base systemd modconf keyboard sd-vconsole block sd-encrypt filesystems fsck" if encrypted else "base systemd modconf keyboard sd-vconsole block filesystems fsck"
        modules = "nvme xhci_pci usb_storage uas usbhid hid_generic mmc_core mmc_block ext4" + (" dm_crypt dm_mod" if encrypted else "")
        fragment = (
            "# Generated for the mounted Omarchy Pi target.\n"
            "# Display setup remains firmware-owned.\n"
            f"MODULES=({modules})\n"
            f"HOOKS=({hooks})\n"
        )
        fragment_path = checked_root / "etc/mkinitcpio.conf.d/90-omarchy-pi-target.conf"
        bootmod._atomic_write(fragment_path, fragment.encode(), mode=0o644)
        bootmod._reject_fat_symlinks(boot)
        boot_runner = _boot_runner(runner, checked_root, boot)
        bootmod._run_native_mkinitcpio(
            checked_root,
            runner=boot_runner,
            machine=machine or platform.machine(),
        )
        generated = boot / "initramfs-linux.img"
        bootmod._regular_file(generated, description="generated target initramfs")
        bootmod._reject_fat_symlinks(boot)
        inspect = _target_exec(
            checked_root,
            boot,
            ["/usr/bin/lsinitcpio", "-l", "/boot/initramfs-linux.img"],
            runner=runner,
        )
        listing = getattr(inspect, "stdout", "") or ""
        builtins = bootmod._target_builtin_modules(checked_root)
        aliases = {
            "nvme": ("nvme.ko", "nvme.ko.xz", "nvme.ko.zst"),
            "xhci_pci": ("xhci-pci.ko", "xhci_pci.ko"),
            "usb_storage": ("usb-storage.ko", "usb_storage.ko"),
            "uas": ("uas.ko",),
            "ext4": ("ext4.ko",),
            "dm_crypt": ("dm-crypt.ko", "dm_crypt.ko"),
            "dm_mod": ("dm-mod.ko", "dm_mod.ko"),
        }
        required = {"nvme", "xhci_pci", "usb_storage", "uas", "ext4"}
        if encrypted:
            required.update({"dm_crypt", "dm_mod"})
        normalized = listing.replace("-", "_")
        missing = [
            item
            for item in sorted(required)
            if item in aliases and not bootmod._module_present(listing, aliases[item], builtins, item)
        ]
        if encrypted and "systemd-cryptsetup" not in normalized and "cryptsetup" not in normalized:
            missing.append("systemd-cryptsetup")
        if missing:
            raise _error("target initramfs is missing required storage support")
        if encrypted and "kms" in listing.lower():
            raise _error("target encrypted initramfs contains kms")
        if storage["key_path"] is not None:
            if ".cryptroot.key" in listing or (root / ".cryptroot.key").exists():
                raise _error("target initramfs contains the external unlock key")
            if any(item.name == ".cryptroot.key" for item in boot.rglob("*")):
                raise _error("target boot contains the external unlock key")
    except TargetProvisionError:
        raise
    except Exception as exc:
        raise _error("target boot configuration failed") from exc


def _host_key_fingerprint(root: Path) -> str:
    candidates = sorted(root.joinpath("etc/ssh").glob("ssh_host_*_key.pub"))
    # Ed25519 is the normal OpenSSH host identity and the one clients will
    # generally negotiate first.  Keep the deterministic sorted fallback for
    # targets where that key type is unavailable.
    candidates.sort(key=lambda path: (0 if path.name == "ssh_host_ed25519_key.pub" else 1, path.name))
    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        parts = path.read_text(encoding="utf-8").strip().split()
        if len(parts) >= 2:
            try:
                blob = base64.b64decode(parts[1], validate=True)
            except (ValueError, binascii.Error):
                continue
            # OpenSSH's SHA256 fingerprint encoding is standard Base64 with
            # padding removed.  It is not the URL-safe Base64 variant.
            digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
            return "SHA256:" + digest
    raise _error("target has no SSH host key")


def _validate_result(
    root: Path,
    account: Account,
    settings: Mapping[str, Any],
    storage: Mapping[str, Any],
    rdp_bind: str,
    runtime_layout: str = "legacy",
) -> str:
    machine_id = _target_path(root, "/etc/machine-id")
    value = machine_id.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", value) or value == "0" * 32:
        raise _error("target machine identity is not fresh")
    if _target_path(root, "/etc/hostname").read_text(encoding="utf-8").strip() != settings["hostname"]:
        raise _error("target hostname does not match settings")
    localtime = _target_path(root, "/etc/localtime")
    expected_localtime = "/usr/share/zoneinfo/" + settings["timezone"]
    if not localtime.is_symlink() or os.readlink(localtime) != expected_localtime:
        raise _error("target timezone link does not match settings")
    fingerprint = _host_key_fingerprint(root)
    for private in root.joinpath("etc/ssh").glob("ssh_host_*_key"):
        if private.is_file() and stat.S_IMODE(private.stat().st_mode) & 0o077:
            raise _error("target SSH host key is too open")
    for marker in _INSTALLER_MARKERS:
        if (root / marker).exists() or (root / marker).is_symlink():
            raise _error("installer state remains in the target")
    for path in root.joinpath("etc/systemd/system").glob("**/*"):
        if path.name in _INSTALLER_UNITS:
            raise _error("installer service remains enabled in the target")
    try:
        passwd = (root / "etc/passwd").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise _error("target passwd database is unreadable") from None
    if any(line.split(":", 1)[0] in _GENERIC_ACCOUNTS for line in passwd.splitlines()):
        raise _error("generic installer account remains in the target")
    account_lines = [line.split(":") for line in passwd.splitlines() if line.startswith(account.username + ":")]
    if len(account_lines) != 1 or len(account_lines[0]) < 7:
        raise _error("selected target account is missing")
    account_fields = account_lines[0]
    try:
        passwd_uid, passwd_gid = int(account_fields[2]), int(account_fields[3])
    except ValueError:
        raise _error("selected target account IDs are invalid") from None
    if passwd_uid != account.uid or passwd_gid != account.gid or account_fields[5] != f"/home/{account.username}":
        raise _error("selected target account identity is invalid")
    shadow_path = _target_path(root, "/etc/shadow")
    if not shadow_path.is_file() or shadow_path.is_symlink():
        raise _error("target shadow database is missing")
    shadow_lines = [line.split(":") for line in shadow_path.read_text(encoding="utf-8").splitlines() if line.startswith(account.username + ":")]
    if len(shadow_lines) != 1 or len(shadow_lines[0]) < 2 or not shadow_lines[0][1] or shadow_lines[0][1].startswith(("!", "*")):
        raise _error("selected target account password is inactive")
    sudoers = _target_path(root, f"/etc/sudoers.d/90-omarchy-pi-{account.username}")
    if not sudoers.is_file() or sudoers.is_symlink() or stat.S_IMODE(sudoers.stat().st_mode) != 0o440:
        raise _error("selected target sudoers file is invalid")
    if sudoers.read_text(encoding="utf-8") != f"{account.username} ALL=(ALL:ALL) ALL\n":
        raise _error("selected target sudoers content is invalid")
    for profile in root.joinpath("etc/NetworkManager/system-connections").glob("*installer*"):
        if profile.exists() or profile.is_symlink():
            raise _error("installer NetworkManager profile remains in the target")
    fstab = _target_path(root, "/etc/fstab").read_text(encoding="utf-8")
    if f"UUID={storage['root_uuid']} / ext4" not in fstab or f"UUID={storage['boot_uuid']} /boot vfat" not in fstab:
        raise _error("target fstab does not identify target filesystems")
    cmdline = _target_path(root, "/boot/cmdline.txt").read_text(encoding="utf-8")
    if storage["luks_uuid"] is None:
        if f"root=UUID={storage['root_uuid']}" not in cmdline or "rd.luks" in cmdline:
            raise _error("target plain boot arguments are invalid")
    else:
        if f"rd.luks.name={storage['luks_uuid']}=cryptroot" not in cmdline or "root=/dev/mapper/cryptroot" not in cmdline:
            raise _error("target encrypted boot arguments are invalid")
        if settings["encryption"] == "key" and f"UUID={storage['key_uuid']}" not in cmdline:
            raise _error("target key boot arguments are invalid")
    if settings["rdp_mode"] == "disabled":
        if os.path.lexists(account.home / ".config/omarchy-pi-rdp"):
            raise _error("disabled target RDP profile remains")
        if os.path.lexists(account.home / ".config/systemd/user/graphical-session.target.wants/omarchy-pi-hypr-rdp.service"):
            raise _error("disabled target RDP service remains enabled")
        if os.path.lexists(_target_path(root, "/etc/omarchy-pi/rdp-profile.toml")):
            raise _error("disabled target RDP policy remains")
    else:
        config_path = account.home / ".config/omarchy-pi-rdp/config.toml"
        tls_path = account.home / ".config/hypr-rdp"
        rdp_wants = account.home / ".config/systemd/user/graphical-session.target.wants/omarchy-pi-hypr-rdp.service"
        expected_rdp_unit = (
            "/usr/lib/systemd/user/omarchy-pi-hypr-rdp.service"
            if runtime_layout == "packaged"
            else "/etc/systemd/user/omarchy-pi-hypr-rdp.service"
        )
        policy_path = _target_path(root, "/etc/omarchy-pi/rdp-profile.toml")
        policy_parent = policy_path.parent
        runtime_validator = _target_path(
            root,
            "/usr/libexec/omarchy-pi/verify-hypr-rdp-runtime.py"
            if runtime_layout == "packaged"
            else "/usr/local/libexec/omarchy-pi/verify-hypr-rdp-runtime.py",
        )
        expected_policy = f'username = {json.dumps(account.username)}\nbind = {json.dumps(rdp_bind)}\n'
        config_text = config_path.read_text(encoding="utf-8")
        validator_bytes = runtime_validator.read_bytes() if runtime_validator.is_file() and not runtime_validator.is_symlink() else b""
        expected_validator_sha256 = hashlib.sha256(_runtime_validator_bytes()).hexdigest()
        try:
            policy_parent_info = policy_parent.lstat()
        except OSError:
            raise _error("target RDP profile parent directory is unavailable") from None
        policy_parent_valid = (
            stat.S_ISDIR(policy_parent_info.st_mode)
            and not stat.S_ISLNK(policy_parent_info.st_mode)
            and stat.S_IMODE(policy_parent_info.st_mode) == 0o755
            and (os.geteuid() != 0 or (policy_parent_info.st_uid == 0 and policy_parent_info.st_gid == 0))
        )
        if (
            rdp_bind not in config_text
            or f'username = {json.dumps(account.username)}' not in config_text
            or not rdp_wants.is_symlink()
            or os.readlink(rdp_wants) != expected_rdp_unit
            or not policy_parent_valid
            or not policy_path.is_file()
            or policy_path.is_symlink()
            or stat.S_IMODE(policy_path.stat().st_mode) != 0o644
            or (os.geteuid() == 0 and policy_path.stat().st_uid != 0)
            or policy_path.read_text(encoding="utf-8") != expected_policy
            or not runtime_validator.is_file()
            or runtime_validator.is_symlink()
            or not (stat.S_IMODE(runtime_validator.stat().st_mode) & 0o111)
            or (os.geteuid() == 0 and runtime_validator.stat().st_uid != 0)
            or (runtime_layout != "packaged" and hashlib.sha256(validator_bytes).hexdigest() != expected_validator_sha256)
            or tls_path.exists()
            or tls_path.is_symlink()
        ):
            raise _error("target RDP profile does not match settings")
    ssh_wants = _target_path(root, "/etc/systemd/system/multi-user.target.wants/sshd.service")
    if settings["ssh_enabled"]:
        if not ssh_wants.is_symlink() or os.readlink(ssh_wants) not in {"/usr/lib/systemd/system/sshd.service", "../sshd.service"}:
            raise _error("target SSH service is not enabled")
    elif os.path.lexists(ssh_wants):
        raise _error("target SSH service is enabled while disabled")
    session_wants = _target_path(
        root,
        f"/etc/systemd/system/multi-user.target.wants/omarchy-pi-uwsm-session@{account.username}.service",
    )
    expected_session_unit = (
        "/usr/lib/systemd/system/omarchy-pi-uwsm-session@.service"
        if runtime_layout == "packaged"
        else "../omarchy-pi-uwsm-session@.service"
    )
    if not session_wants.is_symlink() or os.readlink(session_wants) != expected_session_unit:
        raise _error("target desktop session is not enabled")
    if runtime_layout == "packaged":
        vendor_units = (
            _target_path(root, "/usr/lib/systemd/system/omarchy-pi-uwsm-session@.service"),
            _target_path(root, "/usr/lib/systemd/user/omarchy-pi-hypr-rdp.service"),
        )
        if any(not path.is_file() or path.is_symlink() for path in vendor_units):
            raise _error("packaged target systemd vendor unit is missing")
        shadow_units = (
            _target_path(root, "/etc/systemd/system/omarchy-pi-uwsm-session@.service"),
            _target_path(root, "/etc/systemd/user/omarchy-pi-hypr-rdp.service"),
        )
        if any(path.exists() or path.is_symlink() for path in shadow_units):
            raise _error("packaged target contains an /etc systemd shadow unit")
    if runtime_layout == "packaged":
        runtime_root = _target_path(root, "/usr/share/omarchy")
        marker = runtime_root / ".omarchy-pi-source-commit"
        packaged_marker = runtime_root / ".omarchy-pi-packaged.json"
        setup = runtime_root / "install/arm64/setup-desktop-user.sh"
        env_path = account.home / ".config/uwsm/env.d/90-omarchy-pi"
        try:
            packaged_document = json.loads(packaged_marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            packaged_document = None
        if (
            runtime_root.is_symlink()
            or not runtime_root.is_dir()
            or marker.is_symlink()
            or not marker.is_file()
            or not _SOURCE_REVISION.fullmatch(marker.read_text(encoding="utf-8").strip())
            or packaged_marker.is_symlink()
            or not isinstance(packaged_document, dict)
            or packaged_document.get("runtime_mode", packaged_document.get("mode")) != "packaged"
            or packaged_document.get("source_revision") != marker.read_text(encoding="utf-8").strip()
            or setup.is_symlink()
            or not setup.is_file()
            or not os.access(setup, os.X_OK)
            or env_path.is_symlink()
            or not env_path.is_file()
            or 'export OMARCHY_PATH="/usr/share/omarchy"' not in env_path.read_text(encoding="utf-8")
            or (account.home / ".local/share/omarchy-pi/current").exists()
            or (account.home / ".local/share/omarchy-pi/releases").exists()
        ):
            raise _error("packaged Omarchy runtime layout is incomplete")
    return fingerprint


def provision_target(
    root: Path,
    payload: Path,
    settings: dict,
    storage: dict,
    progress: Progress,
    *,
    runner: Runner = subprocess.run,
    machine: str | None = None,
    provenance: dict | None = None,
) -> dict:
    """Provision one mounted target and return a non-secret reconnect summary."""

    validated_provenance = _validate_provenance(provenance)
    validated = validate_settings(settings)
    validated_storage = _validate_storage(storage, validated)
    root = validated_storage["root"]
    boot = validated_storage["boot"]
    validate_target_options(root, validated)
    source, payload_root, provision_script = _source_and_script(payload, root)
    runtime_layout = _payload_runtime_layout(payload_root, source)
    if _account_already_exists(root, validated["username"]):
        raise _error("selected target account already exists")
    for marker in _INSTALLER_MARKERS:
        if (root / marker).exists() or (root / marker).is_symlink():
            raise _error("target still contains installer state")

    progress("desktop payload")
    provision_command = [
        "/bin/bash",
        os.fspath(provision_script),
        "--rootfs",
        os.fspath(root),
        "--source-checkout",
        os.fspath(source),
        "--payload-dir",
        os.fspath(payload_root),
        "--user",
        validated["username"],
    ]
    if runtime_layout == "packaged":
        # The package pair is installed in the payload root before this leaf;
        # selecting the layout here prevents accidental source-release setup.
        provision_command.extend(["--runtime-layout", "packaged"])
    _run(runner, provision_command)
    _overlay_runtime_validator(root, runtime_layout)
    account = _account_from_target(root, validated["username"])

    progress("target identity")
    _configure_identity(root, validated)
    progress("target account")
    _configure_password(root, boot, account.username, validated["password"], runner=runner)
    _configure_admin(root, account, runner=runner, boot=boot)
    _configure_ssh(root, boot, account, validated, runner=runner)

    progress("regional settings")
    _configure_region(root, boot, validated, runner=runner)
    progress("target network")
    _configure_network(root, validated)

    progress("target pacman keyring")
    _target_exec(root, boot, ["/usr/bin/pacman-key", "--init"], runner=runner)
    _target_exec(root, boot, ["/usr/bin/pacman-key", "--populate", "archlinux", "archlinuxarm"], runner=runner)
    progress("desktop user setup")
    if runtime_layout == "packaged":
        setup_path = "/usr/share/omarchy/install/arm64/setup-desktop-user.sh"
        setup_command = ["/usr/bin/bash", setup_path, "--runtime-layout", "packaged"]
    else:
        setup_path = f"/home/{account.username}/.local/share/omarchy-pi/current/install/arm64/setup-desktop-user.sh"
        setup_command = ["/usr/bin/bash", setup_path]
    _target_exec(root, boot, setup_command, runner=runner, user=account.username)

    progress("target remote desktop")
    rdp_bind = _create_rdp_profile(root, account, validated, runtime_layout)
    progress("target boot")
    _write_fstab(root, validated_storage)
    _write_cmdline(root, validated_storage, validated["encryption"])
    _configure_boot(root, boot, validated_storage, runner=runner, machine=machine)

    progress("target validation")
    fingerprint = _validate_result(root, account, validated, validated_storage, rdp_bind, runtime_layout)
    if validated_provenance is not None:
        progress("provenance")
        _persist_provenance(root, validated_provenance)
    summary = {
        "hostname": validated["hostname"],
        "username": validated["username"],
        "ssh_enabled": validated["ssh_enabled"],
        "ssh_port": 22 if validated["ssh_enabled"] else None,
        "ssh_host_key_fingerprint": fingerprint,
        "rdp_mode": validated["rdp_mode"],
        "rdp_bind": rdp_bind if rdp_bind != "disabled" else None,
        "rdp_port": 3389 if rdp_bind != "disabled" else None,
        "root_uuid": validated_storage["root_uuid"],
        "boot_uuid": validated_storage["boot_uuid"],
        "encryption": validated["encryption"],
        "ip_discovery": "discover the target DHCP lease or use the router; .local is not assumed",
    }
    progress("complete")
    return summary


__all__ = ["TargetProvisionError", "provision_target", "validate_settings", "validate_target_options"]
