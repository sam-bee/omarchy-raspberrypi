#!/usr/bin/python3
"""Create one private, loopback-only RDP profile for the current user."""

from __future__ import annotations

import getpass
import json
import os
import pwd
import stat
import sys
import tempfile
from pathlib import Path


def owned_directory(path: Path, uid: int, *, private: bool = False) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
        raise ValueError(f"{path} must be a real directory owned by the current user")
    mode = stat.S_IMODE(info.st_mode)
    if (private and mode != 0o700) or (not private and mode & 0o022):
        raise ValueError(f"{path} has unsafe permissions")


def create_directory(path: Path, uid: int, *, private: bool = False) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    owned_directory(path, uid, private=private)


def create_file(path: Path, payload: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".omarchy-pi.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
    finally:
        if fd >= 0:
            os.close(fd)
        os.unlink(temporary)


def create_profile(home: Path, uid: int, password: str) -> tuple[Path, Path]:
    if not password or any(char in password for char in "\r\n\x00"):
        raise ValueError("RDP password must be nonempty and contain no control line break")
    owned_directory(home, uid)
    config_root = home / ".config"
    create_directory(config_root, uid)
    profile_dir = config_root / "omarchy-pi-rdp"
    create_directory(profile_dir, uid, private=True)
    config_path = profile_dir / "config.toml"
    password_path = profile_dir / "password"
    if config_path.exists() or config_path.is_symlink() or password_path.exists() or password_path.is_symlink():
        raise FileExistsError("RDP profile or password already exists")

    payload = (
        'bind = "127.0.0.1:3389"\n'
        'username = "omarchy-pi"\n'
        f"password_file = {json.dumps(str(password_path))}\n"
        'resolution = "1280x720"\n'
        "fps = 20\n"
        'egfx_codec = "avc420"\n'
        'audio_mode = "off"\n'
        'file_transfer_mode = "off"\n'
    ).encode()
    password_bytes = password.encode("utf-8")
    create_file(password_path, password_bytes)
    try:
        create_file(config_path, payload)
    except BaseException:
        # This run created password_path with O_EXCL. Refuse to erase a file
        # replaced or edited by another writer while the config was published.
        try:
            current = password_path.lstat()
            if stat.S_ISREG(current.st_mode) and current.st_uid == uid and password_path.read_bytes() == password_bytes:
                password_path.unlink()
        except OSError:
            pass
        raise
    return config_path, password_path


def main() -> None:
    if os.geteuid() == 0 or os.geteuid() != os.getuid() or len(sys.argv) != 1:
        raise ValueError("run as the desktop user without arguments or sudo")
    account = pwd.getpwuid(os.getuid())
    home = Path(account.pw_dir)
    if Path.home() != home:
        raise ValueError("HOME does not match the current login account")
    first = getpass.getpass("New RDP password: ")
    second = getpass.getpass("Repeat RDP password: ")
    if first != second:
        raise ValueError("RDP passwords did not match")
    config_path, _ = create_profile(home, account.pw_uid, first)
    print(f"Created private loopback-only RDP profile: {config_path}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, FileExistsError) as exc:
        print(f"RDP profile setup: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
