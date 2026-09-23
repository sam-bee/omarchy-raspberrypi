#!/usr/bin/python3
"""Fail closed unless the current user's pinned hypr-rdp profile is safe."""

from __future__ import annotations

import argparse
import hashlib
import os
import pwd
import re
import ssl
import stat
import sys
import tomllib
from pathlib import Path
from typing import NoReturn


BINARY = Path("/usr/bin/hypr-rdp")
SHA256_FILE = Path("/usr/share/omarchy-pi/hypr-rdp.sha256")
PACKAGE_OWNER_UID = 0
RUNTIME_ROOT = Path("/run/user")


def fail(message: str) -> NoReturn:
    print(f"hypr-rdp preflight: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", type=int, required=True, help="user-manager UID (%%U)")
    parser.add_argument("--home", required=True, help="user-manager home (${HOME})")
    parser.add_argument("--runtime", required=True, help="user-manager runtime root (%%t)")
    return parser.parse_args(argv)


def path_info(
    path: Path,
    *,
    owner_uid: int,
    mode: int | None = None,
    executable: bool = False,
    non_writable: bool = False,
) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        fail(f"cannot inspect {path}: {exc.strerror}")
    if not stat.S_ISREG(info.st_mode):
        fail(f"{path} is not a regular, non-symlink file")
    if info.st_uid != owner_uid:
        fail(f"{path} is not owned by uid {owner_uid}")
    permissions = stat.S_IMODE(info.st_mode)
    if mode is not None and permissions != mode:
        fail(f"{path} mode is not {mode:04o}")
    if mode is None and non_writable and permissions & 0o022:
        fail(f"{path} is group- or world-writable")
    if executable and not info.st_mode & 0o111:
        fail(f"{path} is not executable")
    return info


def real_directory(path: Path, *, owner_uid: int, mode: int | None = None) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        fail(f"cannot inspect directory {path}: {exc.strerror}")
    if not stat.S_ISDIR(info.st_mode):
        fail(f"{path} is not a real directory")
    if info.st_uid != owner_uid:
        fail(f"directory {path} is not owned by uid {owner_uid}")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        fail(f"directory {path} mode is not {mode:04o}")
    return info


def trusted_package_file(path: Path, *, executable: bool = False) -> os.stat_result:
    if not path.is_absolute():
        fail(f"package path {path} is not absolute")

    # Protect against a writable parent directory allowing a non-root account
    # to replace an otherwise root-owned file between preflight and exec.
    for parent in path.parents:
        real_directory(parent, owner_uid=PACKAGE_OWNER_UID)
        parent_info = parent.lstat()
        if stat.S_IMODE(parent_info.st_mode) & 0o022:
            fail(f"package directory {parent} is group- or world-writable")

    return path_info(
        path,
        owner_uid=PACKAGE_OWNER_UID,
        executable=executable,
        non_writable=True,
    )


def read_regular_file(
    path: Path,
    *,
    owner_uid: int,
    mode: int | None = None,
    non_writable: bool = False,
    max_size: int | None = None,
) -> bytes:
    info = path_info(
        path,
        owner_uid=owner_uid,
        mode=mode,
        non_writable=non_writable,
    )
    if max_size is not None and info.st_size > max_size:
        fail(f"{path} is unexpectedly large")
    try:
        # O_NOFOLLOW closes the lstat/open symlink race for profile files.
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            opened_info = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened_info.st_mode) or opened_info.st_uid != owner_uid:
                fail(f"{path} changed while it was being opened")
            content = stream.read((max_size + 1) if max_size is not None else -1)
    except OSError as exc:
        fail(f"cannot read {path}: {exc.strerror}")
    if max_size is not None and len(content) > max_size:
        fail(f"{path} is unexpectedly large")
    return content


def check_account(uid: int, home_argument: str, runtime_argument: str) -> tuple[Path, Path]:
    actual_uid = os.getuid()
    if uid != actual_uid:
        fail("systemd user-manager UID does not match the service process")
    try:
        account = pwd.getpwuid(actual_uid)
    except KeyError:
        fail("service UID is absent from the passwd database")

    home = Path(account.pw_dir)
    if not home.is_absolute() or home_argument != str(home):
        fail("systemd home does not match the passwd database")
    if os.environ.get("HOME") != str(home):
        fail("HOME does not match the passwd database")
    real_directory(home, owner_uid=actual_uid)
    real_directory(home / ".config", owner_uid=actual_uid)

    runtime = RUNTIME_ROOT / str(actual_uid)
    if runtime_argument != str(runtime):
        fail("systemd runtime directory does not match the service UID")
    if os.environ.get("XDG_RUNTIME_DIR") != str(runtime):
        fail("XDG_RUNTIME_DIR does not match the service UID")
    real_directory(runtime, owner_uid=actual_uid, mode=0o700)
    return home, runtime


def check_wayland_socket(runtime: Path, uid: int) -> str:
    display = os.environ.get("WAYLAND_DISPLAY", "")
    if not display or not re.fullmatch(r"[A-Za-z0-9._-]+", display):
        fail("WAYLAND_DISPLAY is absent or not a simple socket name")
    socket_path = runtime / display
    try:
        socket_info = socket_path.lstat()
    except OSError as exc:
        fail(f"Wayland socket {socket_path} is unavailable: {exc.strerror}")
    if not stat.S_ISSOCK(socket_info.st_mode) or socket_info.st_uid != uid:
        fail("Wayland display path is not the current user's socket")
    return display


def check_package_pin() -> str:
    binary_info = trusted_package_file(BINARY, executable=True)
    manifest_info = trusted_package_file(SHA256_FILE)
    if manifest_info.st_size > 66:
        fail("package SHA-256 manifest must contain one digest line")
    manifest = read_regular_file(
        SHA256_FILE,
        owner_uid=PACKAGE_OWNER_UID,
        non_writable=True,
        max_size=66,
    )
    match = re.fullmatch(rb"([0-9a-f]{64})\n?", manifest)
    if match is None:
        fail("package SHA-256 manifest is not one lowercase digest line")

    # Hash an open descriptor after checking its metadata. The installed path
    # remains root-owned and non-writable, as does each parent directory.
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(BINARY, flags)
        with os.fdopen(fd, "rb") as stream:
            opened_info = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened_info.st_mode) or opened_info.st_uid != PACKAGE_OWNER_UID:
                fail("packaged executable changed while it was being opened")
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        fail(f"cannot hash packaged executable: {exc.strerror}")

    expected_digest = match.group(1).decode("ascii")
    actual_digest = digest.hexdigest()
    if actual_digest != expected_digest:
        fail("packaged executable SHA-256 differs from its package manifest")
    if binary_info.st_size == 0:
        fail("packaged executable is empty")
    return actual_digest


def check_config(home: Path, uid: int) -> tuple[Path, Path]:
    config_dir = home / ".config/omarchy-pi-rdp"
    real_directory(config_dir, owner_uid=uid, mode=0o700)
    config_path = config_dir / "config.toml"
    raw_config = read_regular_file(config_path, owner_uid=uid, mode=0o600, max_size=64 * 1024)
    try:
        config = tomllib.loads(raw_config.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        fail(f"cannot parse the private RDP config ({type(exc).__name__})")
    if "password" in config:
        fail("plaintext password in config is forbidden; use password_file")

    expected = {
        "bind": "127.0.0.1:3389",
        "username": "omarchy-pi",
        "resolution": "1280x720",
        "fps": 20,
        "egfx_codec": "avc420",
        "audio_mode": "off",
        "file_transfer_mode": "off",
    }
    if set(config) != set(expected) | {"password_file"}:
        fail("config fields differ from the reviewed persistent profile")
    for key, value in expected.items():
        if config.get(key) != value:
            fail(f"config field {key} differs from the validated local profile")

    password_path = config_dir / "password"
    password_value = config.get("password_file")
    if not isinstance(password_value, str) or password_value != str(password_path):
        fail("password_file differs from the dedicated persistent credential path")
    password = read_regular_file(
        password_path,
        owner_uid=uid,
        mode=0o600,
        max_size=64 * 1024,
    )
    if not password:
        fail("private password file is empty")
    return config_dir, password_path


def check_tls(home: Path, uid: int) -> bool:
    tls_dir = home / ".config/hypr-rdp"
    try:
        tls_dir.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        fail(f"cannot inspect persistent TLS directory: {exc.strerror}")

    real_directory(tls_dir, owner_uid=uid, mode=0o700)
    cert_path = tls_dir / "cert.pem"
    key_path = tls_dir / "key.pem"
    lock_path = tls_dir / ".tls.lock"
    allowed_names = {
        "cert.pem",
        "key.pem",
        ".tls.lock",
        ".key.pem.tmp",
        ".cert.pem.tmp",
    }
    try:
        entries = {entry.name for entry in tls_dir.iterdir()}
    except OSError as exc:
        fail(f"cannot list persistent TLS directory: {exc.strerror}")
    if entries - allowed_names:
        fail("persistent TLS directory contains an unrecognized entry")
    if ".key.pem.tmp" in entries or ".cert.pem.tmp" in entries:
        fail("an interrupted TLS temporary file exists")

    cert_present = "cert.pem" in entries
    key_present = "key.pem" in entries
    if cert_present != key_present:
        fail("persistent TLS certificate/key pair is incomplete")
    if not cert_present:
        fail("persistent TLS directory exists without a certificate/key pair")

    cert = read_regular_file(cert_path, owner_uid=uid, non_writable=True, max_size=1024 * 1024)
    key = read_regular_file(key_path, owner_uid=uid, mode=0o600, max_size=1024 * 1024)
    if not cert or not key:
        fail("persistent TLS certificate/key pair is empty")
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(
            certfile=str(cert_path), keyfile=str(key_path)
        )
    except (OSError, ssl.SSLError) as exc:
        fail(f"persistent TLS certificate/key pair is invalid ({type(exc).__name__})")

    if ".tls.lock" in entries:
        lock_info = path_info(lock_path, owner_uid=uid, mode=0o600)
        if lock_info.st_size != 0:
            fail("persistent TLS lock file is not empty")
    return True


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    home, runtime = check_account(args.uid, args.home, args.runtime)
    display = check_wayland_socket(runtime, args.uid)
    digest = check_package_pin()
    check_config(home, args.uid)
    tls_exists = check_tls(home, args.uid)
    print(
        "hypr-rdp preflight: OK "
        f"sha256={digest} bind=127.0.0.1:3389 display={display} "
        f"tls={'existing' if tls_exists else 'to-create-or-reuse'}"
    )


if __name__ == "__main__":
    main()
