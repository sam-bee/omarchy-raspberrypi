#!/usr/bin/python3
"""Provision first-boot local access on a freshly built installer image.

This module deliberately takes a root-tree argument in its Python API so it
can be tested without touching the running machine.  The command-line entry
point always uses ``/``.  The builder marker is the boundary that makes this
safe: without the exact root-owned marker, the provisioner refuses to make
any account, hostname, identity, or key changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import secrets
import stat
import subprocess
import tempfile
from typing import Any, Callable, Sequence

from settings import InstallerSettings, SettingsError, load_settings


BUILDER_MARKER = Path("/usr/lib/omarchy-pi/installer-image.marker")
BUILDER_MARKER_CONTENT = b"omarchy-pi-installer-image-v1\n"
SETTINGS_FILE = Path("/boot/installer-settings.toml")
COMPLETION_MARKER = Path("/var/lib/omarchy-pi/access-provisioned")
COMPLETION_MARKER_CONTENT = b"omarchy-pi-access-provisioned-v1\n"


class ProvisionError(RuntimeError):
    """Raised when access provisioning cannot safely complete."""


@dataclass(frozen=True, slots=True)
class Account:
    name: str
    uid: int
    gid: int
    home: str


@dataclass(frozen=True, slots=True)
class ProvisionResult:
    already_provisioned: bool
    created_user: bool


Runner = Callable[..., Any]


def _rooted(root: Path, absolute_path: Path) -> Path:
    if not absolute_path.is_absolute():
        raise ProvisionError("internal path must be absolute")
    return root / absolute_path.relative_to("/")


def _lstat(path: Path, *, description: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError:
        raise ProvisionError(f"{description} is unavailable") from None


def _validate_regular_file(
    path: Path,
    *,
    description: str,
    owner_uid: int,
    max_bytes: int,
) -> os.stat_result:
    info = _lstat(path, description=description)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022:
        raise ProvisionError(f"{description} has unsafe type, ownership, or permissions")
    if info.st_size > max_bytes:
        raise ProvisionError(f"{description} is too large")
    return info


def validate_builder_marker(path: Path, *, owner_uid: int = 0) -> None:
    """Require the exact immutable marker emitted by the image builder."""

    _validate_regular_file(path, description="installer image marker", owner_uid=owner_uid, max_bytes=128)
    try:
        content = path.read_bytes()
    except OSError:
        raise ProvisionError("installer image marker is unreadable") from None
    if content != BUILDER_MARKER_CONTENT:
        raise ProvisionError("installer image marker is invalid")


def _validate_completion_marker(path: Path, *, owner_uid: int) -> None:
    _validate_regular_file(path, description="access completion marker", owner_uid=owner_uid, max_bytes=128)
    try:
        content = path.read_bytes()
    except OSError:
        raise ProvisionError("access completion marker is unreadable") from None
    if content != COMPLETION_MARKER_CONTENT:
        raise ProvisionError("access completion marker is invalid")


def _ensure_directory(path: Path, *, description: str, owner_uid: int, owner_gid: int, mode: int) -> None:
    if os.path.lexists(path):
        info = _lstat(path, description=description)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022:
            raise ProvisionError(f"{description} has unsafe type, ownership, or permissions")
        return
    try:
        path.mkdir(mode=mode, parents=True, exist_ok=False)
    except OSError:
        raise ProvisionError(f"cannot create {description}") from None
    try:
        os.chown(path, owner_uid, owner_gid)
        os.chmod(path, mode)
    except OSError:
        raise ProvisionError(f"cannot secure {description}") from None


def _atomic_write(path: Path, payload: bytes, *, mode: int, owner_uid: int, owner_gid: int, description: str) -> None:
    if os.path.lexists(path):
        info = _lstat(path, description=description)
        if stat.S_ISLNK(info.st_mode):
            raise ProvisionError(f"refusing to replace symlink at {description}")
        if not stat.S_ISREG(info.st_mode):
            raise ProvisionError(f"{description} is not a regular file")
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ProvisionError(f"parent directory for {description} is unsafe")
    temporary: Path | None = None
    descriptor = -1
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".omarchy-pi.", dir=parent)
        temporary = Path(temporary_name)
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, owner_uid, owner_gid)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        os.chmod(path, mode)
    except OSError:
        raise ProvisionError(f"cannot write {description}") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _run(runner: Runner, command: Sequence[str], *, operation: str, input_text: str | None = None, check: bool = True) -> Any:
    try:
        return runner(
            list(command),
            input=input_text,
            text=True,
            capture_output=True,
            check=check,
        )
    except (OSError, subprocess.SubprocessError):
        raise ProvisionError(f"{operation} failed") from None


def _lookup_account(runner: Runner, username: str) -> Account | None:
    result = _run(runner, ["getent", "passwd", username], operation="account lookup", check=False)
    if getattr(result, "returncode", 1) != 0:
        return None
    output = getattr(result, "stdout", "")
    if not isinstance(output, str) or not output.strip():
        return None
    rows = output.splitlines()
    if len(rows) != 1:
        raise ProvisionError("account lookup returned an invalid result")
    fields = rows[0].split(":")
    if len(fields) != 7 or fields[0] != username:
        raise ProvisionError("account lookup returned an invalid result")
    try:
        uid = int(fields[2], 10)
        gid = int(fields[3], 10)
    except ValueError:
        raise ProvisionError("account lookup returned an invalid result") from None
    home = fields[5]
    if uid < 0 or gid < 0 or not home.startswith("/") or any(ord(char) < 0x20 or ord(char) == 0x7F for char in home):
        raise ProvisionError("account lookup returned an invalid result")
    return Account(name=username, uid=uid, gid=gid, home=home)


def _account_home(root: Path, account: Account) -> Path:
    home = _rooted(root, Path(account.home))
    info = _lstat(home, description="installer home")
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != account.uid or info.st_mode & 0o022:
        raise ProvisionError("installer home has unsafe type, ownership, or permissions")
    return home


def _write_authorized_key(root: Path, account: Account, key: str) -> None:
    home = _account_home(root, account)
    ssh_dir = home / ".ssh"
    _ensure_directory(ssh_dir, description="installer SSH directory", owner_uid=account.uid, owner_gid=account.gid, mode=0o700)
    key_path = ssh_dir / "authorized_keys"
    expected = (key + "\n").encode("utf-8")
    if os.path.lexists(key_path):
        info = _lstat(key_path, description="installer authorized_keys")
        if not stat.S_ISREG(info.st_mode) or info.st_uid != account.uid or info.st_mode & 0o077:
            raise ProvisionError("installer authorized_keys has unsafe type, ownership, or permissions")
        try:
            current = key_path.read_bytes()
        except OSError:
            raise ProvisionError("installer authorized_keys is unreadable") from None
        if current != expected:
            raise ProvisionError("installer authorized_keys already contains different data")
        return
    _atomic_write(
        key_path,
        expected,
        mode=0o600,
        owner_uid=account.uid,
        owner_gid=account.gid,
        description="installer authorized_keys",
    )


def _ensure_hostname(root: Path, hostname: str, *, owner_uid: int, owner_gid: int) -> None:
    _atomic_write(
        _rooted(root, Path("/etc/hostname")),
        (hostname + "\n").encode("ascii"),
        mode=0o644,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        description="hostname file",
    )


def _ensure_machine_id(root: Path, *, owner_uid: int, owner_gid: int) -> None:
    machine_id = secrets.token_hex(16).encode("ascii") + b"\n"
    _atomic_write(
        _rooted(root, Path("/etc/machine-id")),
        machine_id,
        mode=0o444,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        description="machine-id",
    )


def _host_key_files(root: Path, *, owner_uid: int = 0) -> list[Path]:
    ssh_dir = _rooted(root, Path("/etc/ssh"))
    if os.path.lexists(ssh_dir):
        info = _lstat(ssh_dir, description="SSH host-key directory")
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022:
            raise ProvisionError("SSH host-key directory has unsafe type, ownership, or permissions")
    else:
        raise ProvisionError("SSH host-key directory is missing")
    return sorted(ssh_dir.glob("ssh_host_*_key"))


def _ensure_host_keys(root: Path, runner: Runner, *, owner_uid: int = 0) -> None:
    private_keys = _host_key_files(root, owner_uid=owner_uid)
    if not private_keys:
        _run(runner, ["ssh-keygen", "-A"], operation="SSH host-key generation")
        private_keys = _host_key_files(root, owner_uid=owner_uid)
    if not private_keys:
        raise ProvisionError("SSH host-key generation produced no keys")
    for private_key in private_keys:
        info = _lstat(private_key, description="SSH host key")
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o077:
            raise ProvisionError("SSH host key has unsafe type, ownership, or permissions")
        public_key = Path(str(private_key) + ".pub")
        public_info = _lstat(public_key, description="SSH host public key")
        if not stat.S_ISREG(public_info.st_mode) or public_info.st_uid != owner_uid or public_info.st_mode & 0o022:
            raise ProvisionError("SSH host public key has unsafe type, ownership, or permissions")


def _lock_accounts(runner: Runner, *, alarm_present: bool) -> None:
    _run(runner, ["passwd", "--lock", "root"], operation="root account lock")
    if alarm_present:
        _run(runner, ["usermod", "--lock", "--expiredate", "1", "alarm"], operation="default alarm account lock")


def _installer_authorization(root: Path, account: Account, runner: Runner, *, owner_uid: int, owner_gid: int) -> None:
    # This applies only while creating a new installer-image account. A
    # key-only SSH login must also be able to submit the fixed install job.
    wrapper = _rooted(root, Path("/usr/local/libexec/omarchy-pi/installer-control"))
    _validate_regular_file(wrapper, description="installer control wrapper", owner_uid=owner_uid, max_bytes=65536)
    directory = _rooted(root, Path("/etc/sudoers.d"))
    _ensure_directory(directory, description="installer sudoers directory", owner_uid=owner_uid, owner_gid=owner_gid, mode=0o750)
    destination = directory / "omarchy-installer"
    if os.path.lexists(destination):
        raise ProvisionError("installer authorization already exists")
    policy = f'{account.name} ALL=(root) NOPASSWD: /usr/local/libexec/omarchy-pi/installer-control ""\n'.encode("ascii")
    fd, name = tempfile.mkstemp(prefix=".installer-authorization.", dir=directory)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o440)
        with os.fdopen(fd, "wb") as stream:
            stream.write(policy)
        _run(runner, ["visudo", "--check", "--file", str(temporary)], operation="installer authorization validation")
        _atomic_write(destination, policy, mode=0o440, owner_uid=owner_uid, owner_gid=owner_gid, description="installer authorization")
    finally:
        temporary.unlink(missing_ok=True)


def provision(
    root: Path = Path("/"),
    *,
    runner: Runner = subprocess.run,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> ProvisionResult:
    """Provision one image root, returning without changes after completion."""

    root = Path(root)
    marker = _rooted(root, BUILDER_MARKER)
    validate_builder_marker(marker, owner_uid=owner_uid)
    completion = _rooted(root, COMPLETION_MARKER)
    if os.path.lexists(completion):
        _validate_completion_marker(completion, owner_uid=owner_uid)
        return ProvisionResult(already_provisioned=True, created_user=False)

    settings_path = _rooted(root, SETTINGS_FILE)
    try:
        settings: InstallerSettings = load_settings(settings_path)
    except SettingsError:
        raise ProvisionError("installer settings are missing or invalid") from None

    selected = settings.username
    selected_account = _lookup_account(runner, selected)
    if selected_account is not None:
        # A prior attempt may have created the account and then failed before
        # recording completion.  Its password state is not safely recoverable
        # from this removable settings file, so require a reflash rather than
        # changing an existing account or claiming first boot completed.
        raise ProvisionError("selected installer account already exists; reflash is required")

    created_user = True
    _run(
        runner,
        ["useradd", "--create-home", "--shell", "/bin/bash", selected],
        operation="installer account creation",
    )
    selected_account = _lookup_account(runner, selected)
    if selected_account is None:
        raise ProvisionError("installer account was not created")
    if settings.ssh.password is not None:
        # The password is supplied on stdin only; it is never an argument.
        _run(
            runner,
            ["chpasswd"],
            input_text=f"{selected}:{settings.ssh.password}\n",
            operation="installer account password setup",
        )
    else:
        _run(runner, ["passwd", "--lock", selected], operation="installer password lock")

    assert selected_account is not None
    _account_home(root, selected_account)
    if settings.ssh.authorized_key is not None:
        _write_authorized_key(root, selected_account, settings.ssh.authorized_key)

    alarm = _lookup_account(runner, "alarm")
    _lock_accounts(runner, alarm_present=alarm is not None)
    _ensure_hostname(root, settings.hostname, owner_uid=owner_uid, owner_gid=owner_gid)
    _ensure_machine_id(root, owner_uid=owner_uid, owner_gid=owner_gid)
    _ensure_host_keys(root, runner, owner_uid=owner_uid)
    _installer_authorization(root, selected_account, runner, owner_uid=owner_uid, owner_gid=owner_gid)

    completion.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        completion,
        COMPLETION_MARKER_CONTENT,
        mode=0o600,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        description="access completion marker",
    )
    return ProvisionResult(already_provisioned=False, created_user=created_user)


def main() -> int:
    if os.geteuid() != 0 or os.getuid() != 0:
        print("provision-access: must run as root", file=os.sys.stderr)
        return 2
    try:
        result = provision()
    except (OSError, ProvisionError):
        print("provision-access: access provisioning failed", file=os.sys.stderr)
        return 1
    if result.already_provisioned:
        print("provision-access: already provisioned")
    else:
        print("provision-access: first-boot access provisioning complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
