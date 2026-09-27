#!/usr/bin/python3
"""Focused tests for installer RDP profile provisioning."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import pwd
import stat
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from settings import InstallerIdentity, InstallerSettings, RdpSettings  # noqa: E402

MODULE_PATH = HERE / "provision-rdp.py"
SPEC = importlib.util.spec_from_file_location("provision_rdp", MODULE_PATH)
assert SPEC and SPEC.loader
provision = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = provision
SPEC.loader.exec_module(provision)


class ProvisionRdpTests(unittest.TestCase):
    def settings(self, password: str = "separate-rdp-pass") -> InstallerSettings:
        return InstallerSettings(
            installer=InstallerIdentity(hostname="pi-installer", username="installer"),
            wifi=None,
            ssh=type("SSH", (), {"authorized_key": None, "password": "login-pass"})(),
            rdp=RdpSettings(password=password),
        )

    def account(self, home: Path) -> provision.UserAccount:
        return provision.UserAccount("installer", os.getuid(), os.getgid(), home)

    def test_first_creation_writes_private_authenticated_direct_lan_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir(mode=0o755)
            account = self.account(home)
            profile = provision.create_profile(self.settings(), account)
            self.assertEqual(profile.config.read_text(encoding="utf-8").splitlines()[0], 'bind = "0.0.0.0:3389"')
            config = profile.config.read_text(encoding="utf-8")
            self.assertIn('username = "installer"', config)
            self.assertIn('resolution = "1280x720"', config)
            self.assertIn("fps = 20", config)
            self.assertNotIn("separate-rdp-pass", config)
            self.assertEqual(profile.password.read_text(encoding="utf-8"), "separate-rdp-pass")
            self.assertEqual(stat.S_IMODE(profile.directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(profile.config.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(profile.password.stat().st_mode), 0o600)
            self.assertTrue(profile.tls.is_dir())
            self.assertEqual(stat.S_IMODE(profile.tls.stat().st_mode), 0o700)

    def test_existing_profile_and_incomplete_tls_fail_without_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir(mode=0o755)
            account = self.account(home)
            first = provision.create_profile(self.settings(), account)
            original = first.password.read_bytes()
            with self.assertRaisesRegex(provision.RdpProvisioningError, "already exists"):
                provision.create_profile(self.settings("changed-rdp-pass"), account)
            self.assertEqual(first.password.read_bytes(), original)

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir(mode=0o755)
            tls = home / ".config/hypr-rdp"
            tls.mkdir(parents=True, mode=0o700)
            (home / ".config").chmod(0o755)
            (tls / "key.pem").write_bytes(b"private")
            (tls / "key.pem").chmod(0o600)
            account = self.account(home)
            with self.assertRaisesRegex(provision.RdpProvisioningError, "incomplete"):
                provision.create_profile(self.settings(), account)
            self.assertFalse((home / ".config/omarchy-installer-rdp").exists())

    def test_existing_complete_tls_pair_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir(mode=0o755)
            tls = home / ".config/hypr-rdp"
            tls.mkdir(parents=True, mode=0o700)
            (home / ".config").chmod(0o755)
            cert = tls / "cert.pem"
            key = tls / "key.pem"
            cert.write_bytes(b"certificate")
            key.write_bytes(b"private-key")
            cert.chmod(0o644)
            key.chmod(0o600)
            before = {path.name: path.read_bytes() for path in (cert, key)}
            profile = provision.create_profile(self.settings(), self.account(home))
            self.assertEqual({path.name: path.read_bytes() for path in (cert, key)}, before)
            self.assertEqual(profile.tls, tls)

    def test_completion_marker_makes_complete_profile_retryable_without_replacing_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir(mode=0o755)
            marker = Path(directory) / "state" / "rdp-provisioned"
            account = self.account(home)
            settings = self.settings()
            first = provision.create_profile(
                settings,
                account,
                completion_marker=marker,
                marker_owner_uid=os.getuid(),
                marker_owner_gid=os.getgid(),
            )
            original_password = first.password.read_bytes()
            self.assertEqual(marker.read_bytes(), provision.COMPLETION_MARKER_CONTENT)
            second = provision.create_profile(
                settings,
                account,
                completion_marker=marker,
                marker_owner_uid=os.getuid(),
                marker_owner_gid=os.getgid(),
            )
            self.assertEqual(second, first)
            self.assertEqual(first.password.read_bytes(), original_password)
            with self.assertRaisesRegex(provision.RdpProvisioningError, "differs"):
                provision.create_profile(
                    self.settings("changed-rdp-pass"),
                    account,
                    completion_marker=marker,
                    marker_owner_uid=os.getuid(),
                    marker_owner_gid=os.getgid(),
                )

    def test_complete_profile_without_marker_is_reconciled_after_interrupted_marker_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir(mode=0o755)
            marker = Path(directory) / "state" / "rdp-provisioned"
            account = self.account(home)
            settings = self.settings()
            first = provision.create_profile(settings, account)
            second = provision.create_profile(
                settings,
                account,
                completion_marker=marker,
                marker_owner_uid=os.getuid(),
                marker_owner_gid=os.getgid(),
            )
            self.assertEqual(second, first)
            self.assertEqual(marker.read_bytes(), provision.COMPLETION_MARKER_CONTENT)

    def test_marker_without_profile_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir(mode=0o755)
            marker = Path(directory) / "state" / "rdp-provisioned"
            marker.parent.mkdir()
            marker.write_bytes(provision.COMPLETION_MARKER_CONTENT)
            marker.chmod(0o600)
            with self.assertRaisesRegex(provision.RdpProvisioningError, "without its profile"):
                provision.create_profile(
                    self.settings(),
                    self.account(home),
                    completion_marker=marker,
                    marker_owner_uid=os.getuid(),
                    marker_owner_gid=os.getgid(),
                )

    def test_invalid_password_and_home_are_rejected_without_secret_echo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir(mode=0o755)
            with self.assertRaisesRegex(provision.RdpProvisioningError, "empty or contains") as context:
                provision.create_profile(self.settings("bad\nsecret"), self.account(home))
            self.assertNotIn("bad", str(context.exception))
            link = Path(directory) / "link"
            link.symlink_to(home, target_is_directory=True)
            with self.assertRaises(provision.RdpProvisioningError):
                provision.create_profile(self.settings(), self.account(link))

    def test_service_is_user_scoped_direct_profile_and_never_contains_password(self) -> None:
        service = (HERE / "omarchy-installer-rdp.service").read_text(encoding="utf-8")
        self.assertIn("After=graphical-session.target", service)
        self.assertIn("BindsTo=graphical-session.target", service)
        self.assertIn("ConditionEnvironment=WAYLAND_DISPLAY", service)
        self.assertIn("ConditionPathIsDirectory=%t", service)
        self.assertIn("UMask=0077", service)
        self.assertIn("--config %h/.config/omarchy-installer-rdp/config.toml", service)
        self.assertNotIn("127.0.0.1:3389", service)
        self.assertNotIn("password =", service)
        self.assertIn("/etc/systemd/user/graphical-session.target.wants/omarchy-installer-rdp.service", service)

    def test_first_boot_unit_orders_after_access_without_becoming_an_ssh_requirement(self) -> None:
        unit = (HERE / "omarchy-pi-provision-rdp.service").read_text(encoding="utf-8")
        self.assertIn("Requires=omarchy-pi-provision-access.service", unit)
        self.assertIn("After=local-fs.target omarchy-pi-provision-access.service", unit)
        self.assertIn("Before=omarchy-installer-launch.service graphical.target", unit)
        self.assertIn("RemainAfterExit=yes", unit)
        self.assertNotIn("ConditionPathExists=!/var/lib/omarchy-pi/rdp-provisioned", unit)
        self.assertIn("/boot/installer-settings.toml", unit)
        self.assertNotIn("RequiredBy=sshd.service", unit)


if __name__ == "__main__":
    unittest.main()
