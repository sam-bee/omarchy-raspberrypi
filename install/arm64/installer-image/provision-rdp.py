#!/usr/bin/python3
"""Create the installer user's authenticated direct-LAN hypr-rdp profile.

The settings document is parsed by the shared, non-evaluating ``settings.py``
module.  This command writes one profile only: a second invocation refuses to
change an existing password or TLS state.  A complete profile can be matched
exactly after a power loss before the completion marker was written.  It never
passes the RDP password to a subprocess and never prints it.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import pwd
import stat
import sys
import tempfile
from typing import Sequence

from settings import InstallerSettings, SettingsError, load_settings


PROFILE_DIRECTORY_NAME = "omarchy-installer-rdp"
TLS_DIRECTORY_NAME = "hypr-rdp"
CONFIG_NAME = "config.toml"
PASSWORD_NAME = "password"
TLS_ALLOWED_NAMES = frozenset(
    {
        "cert.pem",
        "key.pem",
        ".tls.lock",
        ".key.pem.tmp",
        ".cert.pem.tmp",
    }
)
COMPLETION_MARKER = Path("/var/lib/omarchy-pi/rdp-provisioned")
COMPLETION_MARKER_CONTENT = b"omarchy-pi-rdp-provisioned-v1\n"


class RdpProvisioningError(RuntimeError):
    """Raised when creating the first installer RDP profile is unsafe."""


@dataclass(frozen=True, slots=True)
class UserAccount:
    username: str
    uid: int
    gid: int
    home: Path


@dataclass(frozen=True, slots=True)
class ProfilePaths:
    directory: Path
    config: Path
    password: Path
    tls: Path


def _validate_completion_marker(path: Path, *, owner_uid: int) -> None:
    """Require the exact root-owned marker emitted after profile completion."""

    _regular_file(path, name="RDP completion marker", owner_uid=owner_uid, exact_mode=0o600)
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise RdpProvisioningError("RDP completion marker is unreadable") from exc
    if content != COMPLETION_MARKER_CONTENT:
        raise RdpProvisioningError("RDP completion marker is invalid")


def _real_directory(path: Path, *, name: str, owner_uid: int, exact_mode: int | None = None) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise RdpProvisioningError(f"{name} does not exist") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RdpProvisioningError(f"{name} must be a real directory")
    if info.st_uid != owner_uid:
        raise RdpProvisioningError(f"{name} has the wrong owner")
    mode = stat.S_IMODE(info.st_mode)
    if exact_mode is not None and mode != exact_mode:
        raise RdpProvisioningError(f"{name} has unsafe permissions")
    if exact_mode is None and mode & 0o022:
        raise RdpProvisioningError(f"{name} is group- or world-writable")


def _create_directory(
    path: Path,
    *,
    name: str,
    owner_uid: int,
    owner_gid: int,
    actor_uid: int,
    mode: int,
    existing_mode: int | None = None,
) -> None:
    if os.path.lexists(path):
        _real_directory(path, name=name, owner_uid=owner_uid, exact_mode=existing_mode)
        return
    try:
        path.mkdir(mode=mode)
    except FileExistsError as exc:
        raise RdpProvisioningError(f"{name} appeared during first creation") from exc
    if actor_uid == 0 and owner_uid != 0:
        try:
            os.chown(path, owner_uid, owner_gid)
        except OSError as exc:
            raise RdpProvisioningError(f"cannot assign {name} to the installer user") from exc
    _real_directory(path, name=name, owner_uid=owner_uid, exact_mode=existing_mode)


def resolve_account(settings: InstallerSettings) -> UserAccount:
    """Resolve and validate the non-root account selected in settings."""

    try:
        record = pwd.getpwnam(settings.username)
    except KeyError as exc:
        raise RdpProvisioningError("installer user is absent from the passwd database") from exc
    if record.pw_uid <= 0 or record.pw_gid < 0:
        raise RdpProvisioningError("installer user must be a non-root account")
    home = Path(record.pw_dir)
    if not home.is_absolute():
        raise RdpProvisioningError("installer home must be an absolute path")
    try:
        if home.resolve(strict=True) != home:
            raise RdpProvisioningError("installer home contains a symlink path component")
    except FileNotFoundError as exc:
        raise RdpProvisioningError("installer home does not exist") from exc
    _real_directory(home, name="installer home", owner_uid=record.pw_uid)
    return UserAccount(settings.username, record.pw_uid, record.pw_gid, home)


def _check_actor(account: UserAccount) -> int:
    actor_uid = os.geteuid()
    if actor_uid not in {0, account.uid}:
        raise RdpProvisioningError("run as the installer user or root")
    return actor_uid


def _regular_file(path: Path, *, name: str, owner_uid: int, exact_mode: int | None = None, non_writable: bool = False) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise RdpProvisioningError(f"{name} does not exist") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RdpProvisioningError(f"{name} must be a regular file")
    if info.st_uid != owner_uid:
        raise RdpProvisioningError(f"{name} has the wrong owner")
    mode = stat.S_IMODE(info.st_mode)
    if exact_mode is not None and mode != exact_mode:
        raise RdpProvisioningError(f"{name} has unsafe permissions")
    if non_writable and mode & 0o022:
        raise RdpProvisioningError(f"{name} is group- or world-writable")
    return info


def validate_tls_state(home: Path, *, owner_uid: int, owner_gid: int, actor_uid: int) -> Path:
    """Create or validate the persistent hypr-rdp TLS directory without replacing keys."""

    tls_directory = home / ".config" / TLS_DIRECTORY_NAME
    if not os.path.lexists(tls_directory):
        _create_directory(
            tls_directory,
            name="hypr-rdp TLS directory",
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            actor_uid=actor_uid,
            mode=0o700,
        )
        return tls_directory
    _real_directory(tls_directory, name="hypr-rdp TLS directory", owner_uid=owner_uid, exact_mode=0o700)
    try:
        entries = {entry.name for entry in tls_directory.iterdir()}
    except OSError as exc:
        raise RdpProvisioningError("cannot inspect hypr-rdp TLS directory") from exc
    if entries - TLS_ALLOWED_NAMES:
        raise RdpProvisioningError("hypr-rdp TLS directory contains an unexpected entry")
    if ".key.pem.tmp" in entries or ".cert.pem.tmp" in entries:
        raise RdpProvisioningError("hypr-rdp TLS generation has an unfinished temporary file")
    cert_present = "cert.pem" in entries
    key_present = "key.pem" in entries
    if cert_present != key_present:
        raise RdpProvisioningError("hypr-rdp TLS certificate/key pair is incomplete")
    if cert_present:
        _regular_file(tls_directory / "cert.pem", name="hypr-rdp certificate", owner_uid=owner_uid, non_writable=True)
        _regular_file(tls_directory / "key.pem", name="hypr-rdp private key", owner_uid=owner_uid, exact_mode=0o600)
    if ".tls.lock" in entries:
        lock = _regular_file(tls_directory / ".tls.lock", name="hypr-rdp TLS lock", owner_uid=owner_uid, exact_mode=0o600)
        if lock.st_size != 0:
            raise RdpProvisioningError("hypr-rdp TLS lock is not empty")
    return tls_directory


def _atomic_user_file(path: Path, payload: bytes, *, owner_uid: int, owner_gid: int, actor_uid: int) -> None:
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".omarchy-installer-rdp.", dir=path.parent)
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        if actor_uid == 0 and owner_uid != 0:
            os.fchown(descriptor, owner_uid, owner_gid)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
    except FileExistsError as exc:
        raise RdpProvisioningError("RDP profile appeared during first creation") from exc
    except OSError as exc:
        raise RdpProvisioningError("cannot create the private RDP profile") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    _regular_file(path, name="new RDP profile file", owner_uid=owner_uid, exact_mode=0o600)


def _write_completion_marker(
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int,
    actor_uid: int,
) -> None:
    """Write the completion marker only after every profile file is valid."""

    parent = path.parent
    if os.path.lexists(parent):
        _real_directory(parent, name="RDP completion marker directory", owner_uid=owner_uid)
    else:
        _create_directory(
            parent,
            name="RDP completion marker directory",
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            actor_uid=actor_uid,
            mode=0o755,
        )
    if os.path.lexists(path):
        _validate_completion_marker(path, owner_uid=owner_uid)
        return
    _atomic_user_file(
        path,
        COMPLETION_MARKER_CONTENT,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        actor_uid=actor_uid,
    )


def _validate_existing_profile(
    settings: InstallerSettings,
    account: UserAccount,
    profile_directory: Path,
    tls_directory: Path,
) -> ProfilePaths:
    """Accept only the exact profile left by a previously interrupted run."""

    _real_directory(profile_directory, name="installer RDP profile directory", owner_uid=account.uid, exact_mode=0o700)
    password_path = profile_directory / PASSWORD_NAME
    config_path = profile_directory / CONFIG_NAME
    expected_password = settings.rdp.password.encode("utf-8")
    _regular_file(password_path, name="installer RDP password", owner_uid=account.uid, exact_mode=0o600)
    _regular_file(config_path, name="installer RDP config", owner_uid=account.uid, exact_mode=0o600)
    try:
        password = password_path.read_bytes()
        config = config_path.read_bytes()
    except OSError as exc:
        raise RdpProvisioningError("existing installer RDP profile is unreadable") from exc
    if password != expected_password:
        raise RdpProvisioningError("existing installer RDP password differs; refusing to replace it")
    if config != _profile_payload(account.home, account.username):
        raise RdpProvisioningError("existing installer RDP config differs; refusing to replace it")
    return ProfilePaths(profile_directory, config_path, password_path, tls_directory)


def _profile_payload(home: Path, username: str) -> bytes:
    password_path = home / ".config" / PROFILE_DIRECTORY_NAME / PASSWORD_NAME
    return (
        'bind = "0.0.0.0:3389"\n'
        'output = "omarchy-installer"\n'
        f"username = {json.dumps(username)}\n"
        f"password_file = {json.dumps(str(password_path))}\n"
        'resolution = "1280x720"\n'
        "fps = 20\n"
        'egfx_codec = "avc420"\n'
        'audio_mode = "off"\n'
        'file_transfer_mode = "off"\n'
    ).encode("utf-8")


def create_profile(
    settings: InstallerSettings,
    account: UserAccount,
    *,
    completion_marker: Path | None = None,
    marker_owner_uid: int = 0,
    marker_owner_gid: int = 0,
) -> ProfilePaths:
    """Create or safely reconcile one profile and optionally record completion.

    Without ``completion_marker`` a pre-existing profile is rejected.  The
    first-boot service supplies the marker path, which permits retrying only an
    exact, complete profile after a power loss and never overwrites its
    password or TLS state.
    """

    password = settings.rdp.password
    if not isinstance(password, str) or not password or any(ord(char) < 0x20 or ord(char) == 0x7F for char in password):
        raise RdpProvisioningError("validated RDP password is empty or contains a control character")
    actor_uid = _check_actor(account)
    try:
        if account.home.resolve(strict=True) != account.home:
            raise RdpProvisioningError("installer home contains a symlink path component")
    except FileNotFoundError as exc:
        raise RdpProvisioningError("installer home does not exist") from exc
    _real_directory(account.home, name="installer home", owner_uid=account.uid)
    config_root = account.home / ".config"
    _create_directory(
        config_root,
        name="installer .config directory",
        owner_uid=account.uid,
        owner_gid=account.gid,
        actor_uid=actor_uid,
        mode=0o700,
        existing_mode=None,
    )
    profile_directory = config_root / PROFILE_DIRECTORY_NAME
    profile_exists = os.path.lexists(profile_directory)
    marker_exists = completion_marker is not None and os.path.lexists(completion_marker)
    if marker_exists:
        _validate_completion_marker(completion_marker, owner_uid=marker_owner_uid)
        if not profile_exists:
            raise RdpProvisioningError("RDP completion marker exists without its profile")
    # Reconciliation must not create missing TLS state beside an existing
    # profile.  A first run creates TLS before the profile, so its absence on
    # retry means the state is incomplete and must fail closed.
    if profile_exists and not os.path.lexists(account.home / ".config" / TLS_DIRECTORY_NAME):
        raise RdpProvisioningError("existing installer RDP profile has no TLS state")
    tls_directory = validate_tls_state(
        account.home,
        owner_uid=account.uid,
        owner_gid=account.gid,
        actor_uid=actor_uid,
    )
    if profile_exists:
        if completion_marker is None:
            raise RdpProvisioningError("installer RDP profile already exists; refusing to replace it")
        profile = _validate_existing_profile(settings, account, profile_directory, tls_directory)
        if not marker_exists:
            _write_completion_marker(
                completion_marker,
                owner_uid=marker_owner_uid,
                owner_gid=marker_owner_gid,
                actor_uid=actor_uid,
            )
        return profile
    _create_directory(
        profile_directory,
        name="installer RDP profile directory",
        owner_uid=account.uid,
        owner_gid=account.gid,
        actor_uid=actor_uid,
        mode=0o700,
        existing_mode=0o700,
    )
    password_path = profile_directory / PASSWORD_NAME
    config_path = profile_directory / CONFIG_NAME
    try:
        _atomic_user_file(
            password_path,
            password.encode("utf-8"),
            owner_uid=account.uid,
            owner_gid=account.gid,
            actor_uid=actor_uid,
        )
        _atomic_user_file(
            config_path,
            _profile_payload(account.home, account.username),
            owner_uid=account.uid,
            owner_gid=account.gid,
            actor_uid=actor_uid,
        )
    except BaseException:
        for path in (config_path, password_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        try:
            profile_directory.rmdir()
        except OSError:
            pass
        raise
    profile = ProfilePaths(profile_directory, config_path, password_path, tls_directory)
    if completion_marker is not None:
        _write_completion_marker(
            completion_marker,
            owner_uid=marker_owner_uid,
            owner_gid=marker_owner_gid,
            actor_uid=actor_uid,
        )
    return profile


def provision(
    settings_path: Path,
    *,
    completion_marker: Path = COMPLETION_MARKER,
    marker_owner_uid: int = 0,
    marker_owner_gid: int = 0,
) -> ProfilePaths:
    try:
        settings = load_settings(settings_path)
    except SettingsError as exc:
        raise RdpProvisioningError("installer settings are invalid") from exc
    account = resolve_account(settings)
    return create_profile(
        settings,
        account,
        completion_marker=completion_marker,
        marker_owner_uid=marker_owner_uid,
        marker_owner_gid=marker_owner_gid,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", "--settings-file", dest="settings_path", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        profile = provision(args.settings_path)
    except (RdpProvisioningError, OSError) as exc:
        print(f"provision-rdp: error: {exc}", file=sys.stderr)
        return 2
    print(f"created authenticated installer RDP profile: {profile.config}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
