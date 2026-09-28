#!/usr/bin/python3
"""Focused tests for the installer-only headless session launch path."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "launch-installer-session.py"
sys.path.insert(0, str(HERE))
SPEC = importlib.util.spec_from_file_location("launch_installer_session", MODULE_PATH)
assert SPEC and SPEC.loader
launcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = launcher
SPEC.loader.exec_module(launcher)


class InstallerSessionTests(unittest.TestCase):
    def settings_text(self, username: str) -> str:
        return (
            "[installer]\n"
            'hostname = "pi-installer"\n'
            f'username = "{username}"\n\n'
            "[ssh]\n"
            'password = "login-pass"\n\n'
            "[rdp]\n"
            'password = "rdp-pass"\n'
        )

    def test_valid_settings_queue_only_selected_template_without_secret(self) -> None:
        account = pwd.getpwuid(os.getuid())
        if os.getuid() == 0 or not account.pw_name.islower() or not account.pw_name.replace("_", "a").replace("-", "a").isalnum():
            self.skipTest("test account is unsuitable for validated installer username")
        home = Path(account.pw_dir)
        if home.stat().st_uid != os.getuid() or home.stat().st_mode & 0o022:
            self.skipTest("test account home is not suitable")
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "installer-settings.toml"
            settings.write_text(self.settings_text(account.pw_name), encoding="utf-8")
            calls: list[list[str]] = []

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                self.assertNotIn("login-pass", command)
                self.assertNotIn("rdp-pass", command)
                return subprocess.CompletedProcess(command, 0, "", "")

            unit = launcher.launch(settings, runner=runner)
            self.assertEqual(unit, f"omarchy-installer-session@{account.pw_name}.service")
            runtime_unit = f"user-runtime-dir@{account.pw_uid}.service"
            self.assertEqual(
                calls,
                [
                    ["/usr/bin/systemctl", "restart", launcher.USERDB_UNIT],
                    ["/usr/bin/systemctl", "is-active", "--quiet", launcher.USERDB_UNIT],
                    ["/usr/bin/systemctl", "start", runtime_unit],
                    ["/usr/bin/systemctl", "is-active", "--quiet", runtime_unit],
                    ["/usr/bin/systemctl", "start", "--no-block", unit],
                ],
            )

    def test_desktop_queue_is_blocked_when_runtime_directory_is_not_active(self) -> None:
        account = pwd.getpwuid(os.getuid())
        if os.getuid() == 0 or not account.pw_name.islower() or not account.pw_name.replace("_", "a").replace("-", "a").isalnum():
            self.skipTest("test account is unsuitable for validated installer username")
        home = Path(account.pw_dir)
        if home.stat().st_uid != os.getuid() or home.stat().st_mode & 0o022:
            self.skipTest("test account home is not suitable")
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "installer-settings.toml"
            settings.write_text(self.settings_text(account.pw_name), encoding="utf-8")
            calls: list[list[str]] = []
            runtime_unit = f"user-runtime-dir@{account.pw_uid}.service"

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                if command == ["/usr/bin/systemctl", "is-active", "--quiet", runtime_unit]:
                    return subprocess.CompletedProcess(command, 3, "", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            with self.assertRaisesRegex(launcher.SessionLaunchError, "runtime directory is not active"):
                launcher.launch(settings, runner=runner)
            self.assertEqual(
                calls,
                [
                    ["/usr/bin/systemctl", "restart", launcher.USERDB_UNIT],
                    ["/usr/bin/systemctl", "is-active", "--quiet", launcher.USERDB_UNIT],
                    ["/usr/bin/systemctl", "start", runtime_unit],
                    ["/usr/bin/systemctl", "is-active", "--quiet", runtime_unit],
                ],
            )

    def test_userdb_refresh_failure_blocks_runtime_start(self) -> None:
        account = pwd.getpwuid(os.getuid())
        if os.getuid() == 0 or not account.pw_name.islower() or not account.pw_name.replace("_", "a").replace("-", "a").isalnum():
            self.skipTest("test account is unsuitable for validated installer username")
        home = Path(account.pw_dir)
        if home.stat().st_uid != os.getuid() or home.stat().st_mode & 0o022:
            self.skipTest("test account home is not suitable")
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "installer-settings.toml"
            settings.write_text(self.settings_text(account.pw_name), encoding="utf-8")
            calls: list[list[str]] = []

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                return subprocess.CompletedProcess(command, 1 if command[-2:] == ["restart", launcher.USERDB_UNIT] else 0, "", "")

            with self.assertRaisesRegex(launcher.SessionLaunchError, "system user database refresh"):
                launcher.launch(settings, runner=runner)
            self.assertEqual(calls, [["/usr/bin/systemctl", "restart", launcher.USERDB_UNIT]])

    def test_userdb_refresh_must_be_active_before_runtime_start(self) -> None:
        account = pwd.getpwuid(os.getuid())
        if os.getuid() == 0 or not account.pw_name.islower() or not account.pw_name.replace("_", "a").replace("-", "a").isalnum():
            self.skipTest("test account is unsuitable for validated installer username")
        home = Path(account.pw_dir)
        if home.stat().st_uid != os.getuid() or home.stat().st_mode & 0o022:
            self.skipTest("test account home is not suitable")
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "installer-settings.toml"
            settings.write_text(self.settings_text(account.pw_name), encoding="utf-8")
            calls: list[list[str]] = []

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                if command == ["/usr/bin/systemctl", "is-active", "--quiet", launcher.USERDB_UNIT]:
                    return subprocess.CompletedProcess(command, 3, "", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            with self.assertRaisesRegex(launcher.SessionLaunchError, "system user database is not active"):
                launcher.launch(settings, runner=runner)
            self.assertEqual(
                calls,
                [
                    ["/usr/bin/systemctl", "restart", launcher.USERDB_UNIT],
                    ["/usr/bin/systemctl", "is-active", "--quiet", launcher.USERDB_UNIT],
                ],
            )

    def test_invalid_settings_fail_before_systemctl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "installer-settings.toml"
            settings.write_text("[installer]\nusername = \"root\"\n", encoding="utf-8")
            with self.assertRaises(launcher.SessionLaunchError):
                launcher.launch(settings, runner=lambda *args, **kwargs: self.fail("runner called"))

    def test_units_dependency_and_noninteractive_session_contract(self) -> None:
        launch_unit = (HERE / "omarchy-installer-launch.service").read_text(encoding="utf-8")
        self.assertIn("Requires=omarchy-pi-provision-rdp.service", launch_unit)
        self.assertIn("After=local-fs.target omarchy-pi-provision-rdp.service", launch_unit)
        self.assertNotIn("RequiredBy=sshd.service", launch_unit)
        session_unit = (HERE / "omarchy-installer-session@.service").read_text(encoding="utf-8")
        self.assertIn("User=%i", session_unit)
        self.assertIn("PAMName=login", session_unit)
        self.assertIn("TTYPath=/dev/tty8", session_unit)
        self.assertIn("ExecStartPre=+/usr/bin/chvt 8", session_unit)
        self.assertIn("start-installer-session.sh %i", session_unit)

    def test_headless_config_starts_explicit_output_helper(self) -> None:
        config = (HERE / "installer-hyprland.conf").read_text(encoding="utf-8")
        helper = (HERE / "start-installer-desktop.sh").read_text(encoding="utf-8")
        self.assertIn("exec-once = /usr/local/libexec/omarchy-pi/start-installer-desktop.sh", config)
        self.assertIn("hyprctl output create headless omarchy-installer", helper)
        self.assertIn("/usr/bin/foot --app-id=omarchy-installer", helper)
        self.assertLess(helper.index("hyprctl output create headless"), helper.index("/usr/bin/foot"))

    def test_shell_scripts_are_private_entrypoints_with_no_omarchy_shell_start(self) -> None:
        for name in ("start-installer-session.sh", "start-installer-desktop.sh"):
            path = HERE / name
            self.assertTrue(path.read_text(encoding="utf-8").startswith("#!/bin/bash\n"))
            self.assertFalse("omarchy-launch-shell" in path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
