#!/usr/bin/python3
"""Start the installer-only Hyprland/UWSM session for the selected user."""

from __future__ import annotations

import argparse
from pathlib import Path
import os
import pwd
import stat
import subprocess
import sys
from typing import Sequence

from settings import InstallerSettings, SettingsError, load_settings


SETTINGS_FILE = Path("/boot/installer-settings.toml")
SESSION_UNIT = "omarchy-installer-session@{username}.service"
RUNTIME_DIR_UNIT = "user-runtime-dir@{uid}.service"
USERDB_UNIT = "systemd-userdbd.service"


class SessionLaunchError(RuntimeError):
    """Raised when the validated installer session cannot be queued."""


def _settings_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SessionLaunchError("installer settings file is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SessionLaunchError("installer settings file is not a regular file")


def _account(settings: InstallerSettings) -> pwd.struct_passwd:
    try:
        account = pwd.getpwnam(settings.username)
    except KeyError as exc:
        raise SessionLaunchError("selected installer account is absent") from exc
    if account.pw_uid <= 0 or account.pw_gid < 0 or not account.pw_dir.startswith("/"):
        raise SessionLaunchError("selected installer account is invalid")
    home = Path(account.pw_dir)
    try:
        if home.resolve(strict=True) != home:
            raise SessionLaunchError("selected installer home contains a symlink")
        info = home.lstat()
    except OSError as exc:
        raise SessionLaunchError("selected installer home is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != account.pw_uid or info.st_mode & 0o022:
        raise SessionLaunchError("selected installer home is unsafe")
    return account


def launch(
    settings_path: Path,
    *,
    runner=subprocess.run,
) -> str:
    """Validate settings/account data and queue exactly one session instance."""

    _settings_file(settings_path)
    try:
        settings = load_settings(settings_path)
    except SettingsError as exc:
        raise SessionLaunchError("installer settings are invalid") from exc
    account = _account(settings)
    runtime_unit = RUNTIME_DIR_UNIT.format(uid=account.pw_uid)
    unit = SESSION_UNIT.format(username=settings.username)

    try:
        userdb_result = runner(
            ["/usr/bin/systemctl", "restart", USERDB_UNIT],
            text=True,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionLaunchError("could not refresh the system user database") from exc
    if getattr(userdb_result, "returncode", 1) != 0:
        raise SessionLaunchError("systemd rejected the system user database refresh")

    try:
        userdb_active_result = runner(
            ["/usr/bin/systemctl", "is-active", "--quiet", USERDB_UNIT],
            text=True,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionLaunchError("could not verify the system user database") from exc
    if getattr(userdb_active_result, "returncode", 1) != 0:
        raise SessionLaunchError("system user database is not active")

    try:
        runtime_result = runner(
            ["/usr/bin/systemctl", "start", runtime_unit],
            text=True,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionLaunchError("could not start the account runtime directory") from exc
    if getattr(runtime_result, "returncode", 1) != 0:
        raise SessionLaunchError("systemd rejected the account runtime directory")

    try:
        active_result = runner(
            ["/usr/bin/systemctl", "is-active", "--quiet", runtime_unit],
            text=True,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionLaunchError("could not verify the account runtime directory") from exc
    if getattr(active_result, "returncode", 1) != 0:
        raise SessionLaunchError("account runtime directory is not active")

    try:
        result = runner(
            ["/usr/bin/systemctl", "start", "--no-block", unit],
            text=True,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionLaunchError("could not queue installer session") from exc
    if getattr(result, "returncode", 1) != 0:
        raise SessionLaunchError("systemd rejected the installer session")
    return unit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", "--settings-file", dest="settings_path", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if os.geteuid() != 0 or os.getuid() != 0:
        print("launch-installer-session: must run as root", file=sys.stderr)
        return 2
    try:
        unit = launch(args.settings_path)
    except (OSError, SessionLaunchError):
        print("launch-installer-session: could not queue installer session", file=sys.stderr)
        return 1
    print(f"queued installer session: {unit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
