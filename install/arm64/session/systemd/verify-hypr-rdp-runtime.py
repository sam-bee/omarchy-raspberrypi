#!/usr/bin/python3
"""Fail closed unless the tested Sierra hypr-rdp runtime is still intact."""

from __future__ import annotations

import hashlib
import os
import re
import ssl
import stat
import sys
import tomllib
from pathlib import Path
from typing import NoReturn


UID = 1000
HOME = Path("/home/sierra")
RUNTIME = Path("/run/user/1000")
BINARY = Path(
    "/home/sierra/.local/src/hypr-rdp-v0.1.6-build-20260923/target/release/hypr-rdp"
)
BINARY_SHA256 = "b466c691ebf62378a8eaf5498bbd7bb487035732cd96ba3f1be94b67dcd780d1"
CONFIG = Path("/home/sierra/.config/omarchy-pi-rdp/config.toml")
TLS_DIR = Path("/home/sierra/.config/hypr-rdp")


def fail(message: str) -> NoReturn:
    print(f"hypr-rdp preflight: {message}", file=sys.stderr)
    raise SystemExit(1)


def regular_file(
    path: Path, *, mode: int | None = None, executable: bool = False
) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        fail(f"cannot inspect {path}: {exc.strerror}")
    if not stat.S_ISREG(info.st_mode):
        fail(f"{path} is not a regular, non-symlink file")
    if info.st_uid != UID:
        fail(f"{path} is not owned by uid {UID}")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        fail(f"{path} mode is not {mode:04o}")
    if mode is None and stat.S_IMODE(info.st_mode) & 0o022:
        fail(f"{path} is group- or world-writable")
    if executable and not info.st_mode & 0o111:
        fail(f"{path} is not executable")
    return info


def private_directory(path: Path, *, mode: int) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        fail(f"cannot inspect directory {path}: {exc.strerror}")
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        fail(f"{path} is not a real directory")
    if info.st_uid != UID or stat.S_IMODE(info.st_mode) != mode:
        fail(f"{path} must be uid {UID} and mode {mode:04o}")


def main() -> None:
    if os.getuid() != UID or Path.home() != HOME:
        fail("must run as Sierra (uid 1000, /home/sierra)")
    for parent in (HOME, HOME / ".config"):
        try:
            parent_info = parent.lstat()
        except OSError as exc:
            fail(f"cannot inspect config parent {parent}: {exc.strerror}")
        if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != UID:
            fail(f"config parent {parent} is not a Sierra-owned real directory")

    private_directory(RUNTIME, mode=0o700)
    display = os.environ.get("WAYLAND_DISPLAY", "")
    if not display or not re.fullmatch(r"[A-Za-z0-9._-]+", display):
        fail("WAYLAND_DISPLAY is absent or not a simple socket name")
    if os.environ.get("XDG_RUNTIME_DIR") != str(RUNTIME):
        fail("XDG_RUNTIME_DIR is not /run/user/1000")
    socket_path = RUNTIME / display
    try:
        socket_info = socket_path.lstat()
    except OSError as exc:
        fail(f"Wayland socket {socket_path} is unavailable: {exc.strerror}")
    if not stat.S_ISSOCK(socket_info.st_mode) or socket_info.st_uid != UID:
        fail("Wayland display path is not Sierra's socket")

    regular_file(BINARY, executable=True)
    digest = hashlib.sha256(BINARY.read_bytes()).hexdigest()
    if digest != BINARY_SHA256:
        fail("pinned v0.1.6 executable SHA-256 differs")

    private_directory(CONFIG.parent, mode=0o700)
    regular_file(CONFIG, mode=0o600)
    try:
        with CONFIG.open("rb") as stream:
            config = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
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

    password_file_value = config.get("password_file")
    expected_password_file = CONFIG.parent / "password"
    if (
        not isinstance(password_file_value, str)
        or password_file_value != str(expected_password_file)
    ):
        fail("password_file differs from the dedicated persistent credential path")
    password_file = Path(password_file_value)
    password_info = regular_file(password_file, mode=0o600)
    if password_info.st_size == 0:
        fail("private password file is empty")

    # hypr-rdp v0.1.6 stores its automatically generated TLS identity under
    # HOME. Reuse a complete pair across stops and reboots; stop on signs of an
    # interrupted generation instead of letting the server replace files.
    cert_path = TLS_DIR / "cert.pem"
    key_path = TLS_DIR / "key.pem"
    lock_path = TLS_DIR / ".tls.lock"
    if TLS_DIR.exists() or TLS_DIR.is_symlink():
        private_directory(TLS_DIR, mode=0o700)
        allowed_names = {
            "cert.pem",
            "key.pem",
            ".tls.lock",
            ".key.pem.tmp",
            ".cert.pem.tmp",
        }
        try:
            entries = {entry.name for entry in TLS_DIR.iterdir()}
        except OSError as exc:
            fail(f"cannot list persistent TLS directory: {exc.strerror}")
        unknown_entries = entries - allowed_names
        if unknown_entries:
            fail("persistent TLS directory contains an unrecognized entry")
        if ".key.pem.tmp" in entries or ".cert.pem.tmp" in entries:
            fail("an interrupted TLS temporary file exists")

        cert_exists = cert_path.exists() or cert_path.is_symlink()
        key_exists = key_path.exists() or key_path.is_symlink()
        if cert_exists != key_exists:
            fail("persistent TLS certificate/key pair is incomplete")
        if not cert_exists:
            fail("persistent TLS directory exists without a certificate/key pair")
        cert_info = regular_file(cert_path)
        key_info = regular_file(key_path, mode=0o600)
        if cert_info.st_size == 0 or key_info.st_size == 0:
            fail("persistent TLS certificate/key pair is empty")
        try:
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(
                certfile=str(cert_path), keyfile=str(key_path)
            )
        except (OSError, ssl.SSLError) as exc:
            fail(f"persistent TLS certificate/key pair is invalid ({type(exc).__name__})")

        if ".tls.lock" in entries:
            lock_info = regular_file(lock_path, mode=0o600)
            if lock_info.st_size != 0:
                fail("persistent TLS lock file is not empty")

    print(
        "hypr-rdp preflight: OK "
        f"sha256={digest} bind=127.0.0.1:3389 display={display} "
        f"tls={'existing' if TLS_DIR.exists() and cert_path.exists() else 'to-create-or-reuse'}"
    )


if __name__ == "__main__":
    main()
