#!/usr/bin/python3
"""Focused unprivileged tests for installer access provisioning."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("provision-access.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("provision_access", MODULE_PATH)
assert SPEC and SPEC.loader
provision_access = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = provision_access
SPEC.loader.exec_module(provision_access)


PUBLIC_KEY = "ssh-ed25519 " + "A" * 44


def settings_text(*, password: str | None = None, key: str | None = PUBLIC_KEY) -> str:
    ssh = []
    if key is not None:
        ssh.append(f'authorized_key = "{key}"')
    if password is not None:
        ssh.append(f'password = "{password}"')
    return (
        '[installer]\n'
        'hostname = "pi-installer"\n'
        'username = "installer"\n\n'
        '[ssh]\n'
        + "\n".join(ssh)
        + '\n\n[rdp]\npassword = "rdp-password"\n'
    )


class FakeRunner:
    def __init__(self, root: Path, *, selected_present: bool = False, alarm_present: bool = True, fail_command: str | None = None) -> None:
        self.root = root
        self.selected_present = selected_present
        self.alarm_present = alarm_present
        self.fail_command = fail_command
        self.commands: list[list[str]] = []
        self.inputs: list[str | None] = []

    def __call__(self, command: list[str], *, input: str | None, text: bool, capture_output: bool, check: bool) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        self.inputs.append(input)
        if self.fail_command == command[0]:
            raise subprocess.CalledProcessError(1, command)
        if command[:3] == ["getent", "passwd", "installer"]:
            if not self.selected_present:
                return subprocess.CompletedProcess(command, 2, "", "")
            home = self.root / "home/installer"
            home.mkdir(parents=True, exist_ok=True)
            home.chmod(0o755)
            return subprocess.CompletedProcess(command, 0, "installer:x:1001:1001:Installer:/home/installer:/bin/bash\n", "")
        if command[:3] == ["getent", "passwd", "alarm"]:
            if not self.alarm_present:
                return subprocess.CompletedProcess(command, 2, "", "")
            return subprocess.CompletedProcess(command, 0, "alarm:x:1000:1000:Alarm:/home/alarm:/bin/bash\n", "")
        if command[0] == "useradd":
            self.selected_present = True
            home = self.root / "home/installer"
            home.mkdir(parents=True, exist_ok=True)
            home.chmod(0o755)
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[0] == "ssh-keygen":
            ssh = self.root / "etc/ssh"
            (ssh / "ssh_host_ed25519_key").write_bytes(b"private")
            (ssh / "ssh_host_ed25519_key.pub").write_bytes(b"public")
            (ssh / "ssh_host_ed25519_key").chmod(0o600)
            (ssh / "ssh_host_ed25519_key.pub").chmod(0o644)
            return subprocess.CompletedProcess(command, 0, "", "")
        if check:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")


class ProvisionAccessTests(unittest.TestCase):
    def make_root(self, *, password: str | None = None, key: str | None = PUBLIC_KEY, selected_present: bool = False) -> tuple[Path, FakeRunner]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "usr/lib/omarchy-pi").mkdir(parents=True)
        marker = root / provision_access.BUILDER_MARKER.relative_to("/")
        marker.write_bytes(provision_access.BUILDER_MARKER_CONTENT)
        marker.chmod(0o644)
        (root / "boot").mkdir()
        (root / "boot/installer-settings.toml").write_text(settings_text(password=password, key=key), encoding="utf-8")
        (root / "etc/ssh").mkdir(parents=True)
        (root / "etc/ssh").chmod(0o755)
        (root / "var/lib/omarchy-pi").mkdir(parents=True)
        runner = FakeRunner(root, selected_present=selected_present)
        return root, runner

    def provision(self, root: Path, runner: FakeRunner) -> provision_access.ProvisionResult:
        return provision_access.provision(
            root,
            runner=runner,
            owner_uid=__import__("os").getuid(),
            owner_gid=__import__("os").getgid(),
        )

    def test_first_run_creates_user_without_password_in_argv(self) -> None:
        root, runner = self.make_root(password="login-secret")
        result = self.provision(root, runner)
        self.assertFalse(result.already_provisioned)
        self.assertTrue(result.created_user)
        self.assertTrue((root / "home/installer/.ssh/authorized_keys").exists())
        self.assertEqual((root / "home/installer/.ssh/authorized_keys").stat().st_mode & 0o777, 0o600)
        self.assertEqual((root / "etc/hostname").read_text(), "pi-installer\n")
        self.assertEqual(len((root / "etc/machine-id").read_text().strip()), 32)
        self.assertTrue((root / "etc/ssh/ssh_host_ed25519_key").exists())
        self.assertTrue((root / "var/lib/omarchy-pi/access-provisioned").exists())
        self.assertTrue(all("login-secret" not in command for command in runner.commands))
        self.assertIn("installer:login-secret\n", runner.inputs)
        self.assertIn(["passwd", "--lock", "root"], runner.commands)
        self.assertIn(["usermod", "--lock", "--expiredate", "1", "alarm"], runner.commands)

    def test_key_only_account_is_password_locked_and_no_secret_is_passed(self) -> None:
        root, runner = self.make_root(key=PUBLIC_KEY)
        self.provision(root, runner)
        self.assertIn(["passwd", "--lock", "installer"], runner.commands)
        self.assertNotIn("chpasswd", [command[0] for command in runner.commands])

    def test_completion_marker_makes_rerun_a_noop(self) -> None:
        root, runner = self.make_root(password="first-secret")
        first = self.provision(root, runner)
        machine_id = (root / "etc/machine-id").read_text()
        command_count = len(runner.commands)
        second = self.provision(root, runner)
        self.assertFalse(first.already_provisioned)
        self.assertTrue(second.already_provisioned)
        self.assertEqual((root / "etc/machine-id").read_text(), machine_id)
        self.assertEqual(len(runner.commands), command_count)

    def test_invalid_or_missing_builder_marker_fails_closed(self) -> None:
        root, runner = self.make_root()
        marker = root / provision_access.BUILDER_MARKER.relative_to("/")
        marker.write_text("wrong\n", encoding="ascii")
        with self.assertRaises(provision_access.ProvisionError):
            self.provision(root, runner)
        marker.unlink()
        with self.assertRaises(provision_access.ProvisionError):
            self.provision(root, runner)

    def test_invalid_settings_fail_without_mutation(self) -> None:
        root, runner = self.make_root()
        (root / "boot/installer-settings.toml").write_text("[installer]\nusername = \"root\"\n", encoding="utf-8")
        with self.assertRaises(provision_access.ProvisionError):
            self.provision(root, runner)
        self.assertEqual(runner.commands, [])
        self.assertFalse((root / "etc/hostname").exists())

    def test_missing_settings_fail_without_mutation(self) -> None:
        root, runner = self.make_root()
        (root / "boot/installer-settings.toml").unlink()
        with self.assertRaises(provision_access.ProvisionError):
            self.provision(root, runner)
        self.assertEqual(runner.commands, [])
        self.assertFalse((root / "etc/hostname").exists())

    def test_existing_account_fails_closed_before_mutation(self) -> None:
        root, runner = self.make_root(password="must-not-be-used", selected_present=True)
        with self.assertRaises(provision_access.ProvisionError):
            self.provision(root, runner)
        self.assertNotIn("chpasswd", [command[0] for command in runner.commands])
        self.assertFalse((root / "var/lib/omarchy-pi/access-provisioned").exists())

    def test_partial_creation_retry_refuses_existing_account(self) -> None:
        root, runner = self.make_root(password="partial-secret")
        runner.fail_command = "chpasswd"
        with self.assertRaises(provision_access.ProvisionError):
            self.provision(root, runner)
        self.assertTrue(runner.selected_present)
        self.assertFalse((root / "var/lib/omarchy-pi/access-provisioned").exists())
        runner.fail_command = None
        command_count = len(runner.commands)
        with self.assertRaises(provision_access.ProvisionError):
            self.provision(root, runner)
        self.assertEqual(len(runner.commands), command_count + 1)
        self.assertEqual(runner.commands[-1], ["getent", "passwd", "installer"])
        self.assertNotIn("chpasswd", [command[0] for command in runner.commands[command_count:]])

    def test_marker_ownership_and_permissions_are_checked(self) -> None:
        root, runner = self.make_root()
        marker = root / provision_access.BUILDER_MARKER.relative_to("/")
        marker.chmod(0o666)
        with self.assertRaises(provision_access.ProvisionError):
            self.provision(root, runner)

    def test_unit_orders_access_before_network_and_sshd(self) -> None:
        unit = Path(__file__).with_name("omarchy-pi-provision-access.service").read_text(encoding="utf-8")
        self.assertIn("Before=network-pre.target systemd-networkd.service NetworkManager.service wpa_supplicant.service sshd.service", unit)
        self.assertIn("RequiredBy=network-pre.target sshd.service", unit)
        self.assertIn("AssertPathExists=/usr/lib/omarchy-pi/installer-image.marker", unit)
        self.assertIn("ConditionPathExists=!/var/lib/omarchy-pi/access-provisioned", unit)


if __name__ == "__main__":
    unittest.main()
