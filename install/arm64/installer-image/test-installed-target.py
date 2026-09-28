#!/usr/bin/python3
"""Focused tests for mounted-target provisioning boundaries."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("installed_target", HERE / "installed_target.py")
assert SPEC and SPEC.loader
installed = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installed
SPEC.loader.exec_module(installed)


PUBLIC_KEY = "ssh-ed25519 " + ("A" * 43) + "="


class FakeRunner:
    def __init__(self, root: Path, boot: Path) -> None:
        self.root = root
        self.boot = boot
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []

    def __call__(
        self,
        command: list[str],
        *,
        input: str | None,
        text: bool,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        self.inputs.append(input)
        if command and command[0] == "/bin/bash" and "--rootfs" in command:
            # The real leaf uses useradd --root.  The fixture simulates its
            # result and intentionally has no host account utility at all.
            passwd = self.root / "etc/passwd"
            passwd.write_text(
                passwd.read_text(encoding="utf-8")
                + "desk:x:1001:1001:Desktop:/home/desk:/bin/bash\n",
                encoding="utf-8",
            )
            home = self.root / "home/desk"
            (home / ".local/share/omarchy-pi/current/install/arm64").mkdir(parents=True)
            os.chown(home, os.getuid(), os.getgid())
            wants = self.root / "etc/systemd/system/multi-user.target.wants/omarchy-pi-uwsm-session@desk.service"
            wants.parent.mkdir(parents=True, exist_ok=True)
            wants.symlink_to("../omarchy-pi-uwsm-session@.service")
            return subprocess.CompletedProcess(command, 0, "", "")

        if command and command[0] == "systemctl":
            self._assert_no_daemon_start(command)
            wants = self.root / "etc/systemd/system/multi-user.target.wants/sshd.service"
            wants.parent.mkdir(parents=True, exist_ok=True)
            if not wants.exists() and not wants.is_symlink():
                wants.symlink_to("/usr/lib/systemd/system/sshd.service")
            return subprocess.CompletedProcess(command, 0, "", "")

        if command and command[0] == "systemd-nspawn":
            self._assert_nspawn_target(command)
            inner = command[command.index("--") + 1 :]
            if inner[:2] == ["/usr/bin/locale", "-a"]:
                return subprocess.CompletedProcess(command, 0, "C.UTF-8\n", "")
            if inner[:2] == ["/usr/bin/localectl", "list-keymaps"]:
                return subprocess.CompletedProcess(command, 0, "gb\n", "")
            if inner[:2] == ["/usr/bin/ssh-keygen", "-A"]:
                ssh = self.root / "etc/ssh"
                ssh.mkdir(parents=True, exist_ok=True)
                (ssh / "ssh_host_ed25519_key").write_bytes(b"private")
                (ssh / "ssh_host_ed25519_key.pub").write_text(PUBLIC_KEY + " fixture\n", encoding="ascii")
                (ssh / "ssh_host_ed25519_key").chmod(0o600)
                (ssh / "ssh_host_ed25519_key.pub").chmod(0o644)
            if inner and inner[0] == "/usr/bin/chpasswd":
                self.assert_secret_only_on_stdin(input)
                shadow = self.root / "etc/shadow"
                rows = []
                for line in shadow.read_text(encoding="utf-8").splitlines():
                    if line.startswith("desk:"):
                        rows.append("desk:$6$fixture$active-password-hash:::::::")
                    else:
                        rows.append(line)
                shadow.write_text("\n".join(rows) + "\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        if command and command[0] in {"useradd", "usermod", "chpasswd", "ssh-keygen"}:
            raise AssertionError("host account or host identity command was invoked")
        return subprocess.CompletedProcess(command, 0, "", "")

    def assert_secret_only_on_stdin(self, value: str | None) -> None:
        assert value is not None and value.startswith("desk:")
        assert "target-login-secret" in value

    def _assert_nspawn_target(self, command: list[str]) -> None:
        assert "--network-namespace-path=/proc/1/ns/net" in command
        assert "--resolv-conf=replace-host" in command
        assert f"--bind={self.boot}:/boot" in command
        assert command[command.index("--directory") + 1] == str(self.root)
        assert command.index("--") > command.index("--directory")

    @staticmethod
    def _assert_no_daemon_start(command: list[str]) -> None:
        assert "start" not in command and "--now" not in command


class InstalledTargetTests(unittest.TestCase):
    def test_validate_settings_rejects_unknown_fields_and_secret_echo(self) -> None:
        settings = {
            "username": "desk",
            "hostname": "pi-target",
            "password": "target-login-secret",
            "timezone": "Europe/London",
            "locale": "C.UTF-8",
            "keymap": "gb",
            "wifi": None,
            "ssh_enabled": True,
            "ssh_authorized_key": PUBLIC_KEY,
            "rdp_mode": "disabled",
            "rdp_password": None,
            "encryption": "plain",
            "recovery_passphrase": None,
        }
        validated = installed.validate_settings(settings)
        self.assertEqual(validated["username"], "desk")
        with self.assertRaises(installed.TargetProvisionError) as context:
            installed.validate_settings({**settings, "unexpected": "target-login-secret"})
        self.assertNotIn("target-login-secret", str(context.exception))

    def test_validate_target_options_is_read_only_and_checks_target_sources(self) -> None:
        temporary, root, boot, payload, settings, storage = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
        validated = installed.validate_target_options(root, settings)
        after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
        self.assertEqual(validated["timezone"], "Europe/London")
        self.assertEqual(before, after)
        with self.assertRaises(installed.TargetProvisionError):
            installed.validate_target_options(root, {**settings, "keymap": "missing"})

    def test_storage_accepts_fat_serial_uuid_and_rejects_nonfat_forms(self) -> None:
        temporary, root, boot, payload, settings, storage = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        storage = {**storage, "boot_uuid": "ABCD-1234"}
        checked = installed._validate_storage(storage, installed.validate_settings(settings))
        self.assertEqual(checked["boot_uuid"], "ABCD-1234")
        installed._write_fstab(root, checked)
        self.assertIn("UUID=ABCD-1234 /boot vfat", (root / "etc/fstab").read_text(encoding="utf-8"))
        for invalid in ("abcd1234", "ABCD1234", "0000-0000", "ABCD-1234-5678"):
            with self.subTest(invalid=invalid), self.assertRaises(installed.TargetProvisionError):
                installed._validate_storage({**storage, "boot_uuid": invalid}, installed.validate_settings(settings))

    def test_wifi_profile_persists_target_country_without_host_network_commands(self) -> None:
        temporary, root, boot, payload, settings, storage = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        validated = installed.validate_settings(
            {**settings, "wifi": {"country": "GB", "ssid": "target-wifi", "password": "wifi-secret"}}
        )
        installed._configure_network(root, validated)
        profile = root / "etc/NetworkManager/system-connections/20-omarchy-pi-target-wifi.nmconnection"
        self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
        text = profile.read_text(encoding="utf-8")
        self.assertIn("ssid=target-wifi", text)
        self.assertIn("psk=wifi-secret", text)
        self.assertIn("ieee80211_regdom=GB", (root / "etc/modprobe.d/omarchy-pi-regdom.conf").read_text())

    def make_fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Path, Path, Path, dict, dict]:
        temporary = tempfile.TemporaryDirectory(prefix="omarchy-installed-target-")
        base = Path(temporary.name)
        root = base / "target"
        boot = root / "boot"
        payload = base / "payload"
        source = payload / "source"
        for directory in (
            root / "etc/ssh",
            root / "etc/systemd/system/multi-user.target.wants",
            root / "etc/sudoers.d",
            root / "etc/NetworkManager/system-connections",
            root / "usr/share/zoneinfo/Europe",
            root / "usr/share/i18n/locales",
            root / "usr/share/kbd/keymaps",
            root / "usr/bin",
            boot,
            source / "install/arm64",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        (root / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/bash\n", encoding="utf-8")
        (root / "etc/group").write_text("root:x:0:\n", encoding="utf-8")
        (root / "etc/shadow").write_text("root:!:1::::::\ndesk:!:1::::::\n", encoding="utf-8")
        (root / "usr/share/zoneinfo/Europe/London").write_bytes(b"tz")
        (root / "usr/share/i18n/locales/C").write_bytes(b"locale")
        (root / "usr/share/kbd/keymaps/gb.map.gz").write_bytes(b"keymap")
        (source / "install/arm64/provision-desktop-root.sh").write_text("#!/bin/bash\n", encoding="ascii")
        storage = {
            "root": root,
            "boot": boot,
            "root_uuid": "11111111-1111-1111-1111-111111111111",
            "boot_uuid": "ABCD-1234",
            "luks_uuid": None,
            "key_uuid": None,
            "key_path": None,
        }
        settings = {
            "username": "desk",
            "hostname": "pi-target",
            "password": "target-login-secret",
            "timezone": "Europe/London",
            "locale": "C.UTF-8",
            "keymap": "gb",
            "wifi": None,
            "ssh_enabled": True,
            "ssh_authorized_key": PUBLIC_KEY,
            "rdp_mode": "loopback",
            "rdp_password": "target-rdp-secret",
            "encryption": "plain",
            "recovery_passphrase": None,
        }
        return temporary, root, boot, payload, settings, storage

    def test_provision_uses_target_context_and_keeps_secrets_out_of_summary(self) -> None:
        temporary, root, boot, payload, settings, storage = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        runner = FakeRunner(root, boot)
        original = installed._configure_boot
        installed._configure_boot = lambda *args, **kwargs: None
        self.addCleanup(lambda: setattr(installed, "_configure_boot", original))
        progress: list[str] = []

        summary = installed.provision_target(
            root,
            payload,
            settings,
            storage,
            progress.append,
            runner=runner,
            machine="aarch64",
        )

        self.assertEqual(summary["username"], "desk")
        self.assertEqual(summary["rdp_bind"], "127.0.0.1:3389")
        rdp_config = (root / "home/desk/.config/omarchy-pi-rdp/config.toml").read_text(encoding="utf-8")
        self.assertIn('password_file = "/home/desk/.config/omarchy-pi-rdp/password"', rdp_config)
        self.assertNotIn(str(root), rdp_config)
        self.assertNotIn("target-login-secret", repr(summary))
        self.assertNotIn("target-rdp-secret", repr(summary))
        self.assertNotIn("target-login-secret", " ".join(progress))
        self.assertTrue(any(call[0] == "systemd-nspawn" for call in runner.calls))
        self.assertTrue(any(value and "target-login-secret" in value for value in runner.inputs))
        for call in runner.calls:
            self.assertNotIn("target-login-secret", call)
            self.assertNotIn("target-rdp-secret", call)
        self.assertIn("UUID=11111111-1111-1111-1111-111111111111", (root / "etc/fstab").read_text())
        self.assertIn("root=UUID=11111111-1111-1111-1111-111111111111", (boot / "cmdline.txt").read_text())
        self.assertEqual((root / "etc/hostname").read_text(), "pi-target\n")
        self.assertTrue((root / "home/desk/.ssh/authorized_keys").exists())

    def test_encrypted_cmdline_has_uuid_arguments_without_key_material(self) -> None:
        temporary, root, boot, payload, settings, storage = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        settings = {**settings, "encryption": "key", "recovery_passphrase": "recovery-secret"}
        storage = {
            **storage,
            "luks_uuid": "33333333-3333-3333-3333-333333333333",
            "key_uuid": "44444444-4444-4444-4444-444444444444",
            # disk_install returns the public path in the disposable key
            # filesystem.  The provisioner must never read a host key path.
            "key_path": "/.cryptroot.key",
        }
        validated = installed.validate_settings(settings)
        checked = installed._validate_storage(storage, validated)
        installed._write_cmdline(root, checked, "key")
        cmdline = (boot / "cmdline.txt").read_text()
        self.assertIn("rd.luks.name=33333333-3333-3333-3333-333333333333=cryptroot", cmdline)
        self.assertIn("rd.luks.key=33333333-3333-3333-3333-333333333333=/.cryptroot.key:UUID=44444444-4444-4444-4444-444444444444", cmdline)
        self.assertNotIn("fixture-unlock-key", cmdline)
        self.assertNotIn("recovery-secret", cmdline)

    def test_host_fingerprint_matches_openssh_standard_base64(self) -> None:
        if shutil.which("ssh-keygen") is None:
            self.skipTest("ssh-keygen is unavailable")
        temporary = tempfile.TemporaryDirectory(prefix="omarchy-host-key-")
        self.addCleanup(temporary.cleanup)
        private = Path(temporary.name) / "fixture"
        generated = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(generated.returncode, 0, generated.stderr)
        root = Path(temporary.name) / "root"
        (root / "etc/ssh").mkdir(parents=True)
        shutil.copyfile(private.with_name("fixture.pub"), root / "etc/ssh/ssh_host_ed25519_key.pub")
        expected_output = subprocess.run(
            ["ssh-keygen", "-lf", str(private.with_name("fixture.pub")), "-E", "sha256"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        expected = next(item for item in expected_output if item.startswith("SHA256:"))
        self.assertEqual(installed._host_key_fingerprint(root), expected)


if __name__ == "__main__":
    unittest.main()
