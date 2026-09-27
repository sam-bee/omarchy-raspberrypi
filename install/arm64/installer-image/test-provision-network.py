#!/usr/bin/python3
"""Focused unprivileged tests for installer NetworkManager provisioning."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

try:
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib
except (ImportError, ValueError):
    GLib = None  # type: ignore[assignment]


MODULE_PATH = Path(__file__).with_name("provision-network.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("provision_network", MODULE_PATH)
assert SPEC and SPEC.loader
provision_network = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = provision_network
SPEC.loader.exec_module(provision_network)


PUBLIC_KEY = "ssh-ed25519 " + "A" * 44


def settings_text(*, wifi: bool = True, ssid: str = "test-network", password: str = "wifi-password") -> str:
    def toml_string(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    wifi_block = ""
    if wifi:
        wifi_block = f'\n[wifi]\ncountry = "GB"\nssid = "{toml_string(ssid)}"\npassword = "{toml_string(password)}"\n'
    return (
        '[installer]\n'
        'hostname = "pi-installer"\n'
        'username = "installer"\n'
        + wifi_block
        + '\n[ssh]\n'
        + f'authorized_key = "{PUBLIC_KEY}"\n'
        + '\n[rdp]\npassword = "rdp-password"\n'
    )


class FakeRunner:
    def __init__(self, *, country_success: bool = True) -> None:
        self.country_success = country_success
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], *, input: str | None, text: bool, capture_output: bool, check: bool) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        return subprocess.CompletedProcess(command, 0 if self.country_success else 1, "", "")


class ProvisionNetworkTests(unittest.TestCase):
    def make_root(self, *, wifi: bool = True, ssid: str = "test-network", password: str = "wifi-password") -> tuple[Path, FakeRunner]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        marker = root / provision_network.BUILDER_MARKER.relative_to("/")
        marker.parent.mkdir(parents=True)
        marker.write_bytes(provision_network.BUILDER_MARKER_CONTENT)
        marker.chmod(0o644)
        boot = root / "boot"
        boot.mkdir()
        (boot / "installer-settings.toml").write_text(settings_text(wifi=wifi, ssid=ssid, password=password), encoding="utf-8")
        return root, FakeRunner()

    def provision(self, root: Path, runner: FakeRunner) -> provision_network.NetworkProvisionResult:
        return provision_network.provision(
            root,
            runner=runner,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    def test_wired_fallback_and_wifi_profiles_are_private_and_interface_agnostic(self) -> None:
        root, runner = self.make_root()
        result = self.provision(root, runner)
        self.assertTrue(result.ethernet_profile_written)
        self.assertTrue(result.wifi_profile_written)
        ethernet = root / provision_network.ETHERNET_PROFILE.relative_to("/")
        wifi = root / provision_network.WIFI_PROFILE.relative_to("/")
        self.assertEqual(ethernet.stat().st_mode & 0o777, 0o600)
        self.assertEqual(wifi.stat().st_mode & 0o777, 0o600)
        ethernet_text = ethernet.read_text()
        wifi_text = wifi.read_text()
        self.assertIn("type=ethernet", ethernet_text)
        self.assertIn("method=auto", ethernet_text)
        self.assertIn("ssid=test-network", wifi_text)
        self.assertIn("security=802-11-wireless-security", wifi_text)
        self.assertIn("psk=wifi-password", wifi_text)
        self.assertNotIn("interface-name=", wifi_text)
        self.assertEqual(runner.commands, [["iw", "reg", "set", "GB"]])

    def test_wifi_keyfile_escapes_backslashes_without_logging_password(self) -> None:
        root, runner = self.make_root(ssid=r"ssid\name", password=r"pass\word")
        self.provision(root, runner)
        wifi_text = (root / provision_network.WIFI_PROFILE.relative_to("/")).read_text()
        self.assertIn(r"ssid=ssid\\name", wifi_text)
        self.assertIn(r"psk=pass\\word", wifi_text)
        self.assertNotIn(r"pass\word", runner.commands)

    @unittest.skipIf(GLib is None, "PyGObject GLib is unavailable")
    def test_wifi_keyfile_round_trips_glib_special_values(self) -> None:
        ssid = " edge;#=\\é "
        password = " pass;#=\\word "
        payload = provision_network.wifi_keyfile(
            provision_network.WifiSettings(country="GB", ssid=ssid, password=password)
        ).decode("utf-8")
        keyfile = GLib.KeyFile()
        keyfile.load_from_data(payload, len(payload.encode("utf-8")), GLib.KeyFileFlags.NONE)
        self.assertEqual(keyfile.get_string("wifi", "ssid"), ssid)
        self.assertEqual(keyfile.get_string("wifi-security", "psk"), password)

    def test_ethernet_only_removes_stale_wifi_profile(self) -> None:
        root, runner = self.make_root()
        self.provision(root, runner)
        (root / "boot/installer-settings.toml").write_text(settings_text(wifi=False), encoding="utf-8")
        runner.commands.clear()
        result = self.provision(root, runner)
        self.assertFalse(result.wifi_profile_written)
        self.assertTrue((root / provision_network.ETHERNET_PROFILE.relative_to("/")).exists())
        self.assertFalse((root / provision_network.WIFI_PROFILE.relative_to("/")).exists())
        self.assertEqual(runner.commands, [])

    def test_changed_fat_settings_replace_only_the_owned_wifi_profile(self) -> None:
        root, runner = self.make_root(ssid="old-network", password="old-password")
        self.provision(root, runner)
        (root / "boot/installer-settings.toml").write_text(
            settings_text(ssid="new-network", password="new-password"),
            encoding="utf-8",
        )
        self.provision(root, runner)
        wifi_text = (root / provision_network.WIFI_PROFILE.relative_to("/")).read_text()
        self.assertIn("ssid=new-network", wifi_text)
        self.assertIn("psk=new-password", wifi_text)
        self.assertNotIn("old-network", wifi_text)
        self.assertNotIn("old-password", wifi_text)

    def test_country_failure_is_best_effort(self) -> None:
        root, runner = self.make_root()
        runner.country_success = False
        result = self.provision(root, runner)
        self.assertFalse(result.country_applied)
        self.assertTrue((root / provision_network.WIFI_PROFILE.relative_to("/")).exists())

    def test_invalid_settings_and_marker_fail_without_profiles(self) -> None:
        root, runner = self.make_root()
        (root / "boot/installer-settings.toml").write_text("not valid =", encoding="utf-8")
        with self.assertRaises(provision_network.NetworkProvisionError):
            self.provision(root, runner)
        self.assertFalse((root / provision_network.ETHERNET_PROFILE.relative_to("/")).exists())
        marker = root / provision_network.BUILDER_MARKER.relative_to("/")
        marker.unlink()
        with self.assertRaises(provision_network.NetworkProvisionError):
            self.provision(root, runner)

    def test_unit_orders_network_before_networkmanager_and_ssh(self) -> None:
        unit = Path(__file__).with_name("omarchy-pi-provision-network.service").read_text(encoding="utf-8")
        self.assertIn("Requires=omarchy-pi-provision-access.service", unit)
        self.assertIn("Before=network-pre.target NetworkManager.service sshd.service", unit)
        self.assertIn("RequiredBy=network-pre.target NetworkManager.service sshd.service", unit)
        self.assertIn("AssertPathExists=/usr/lib/omarchy-pi/installer-image.marker", unit)


if __name__ == "__main__":
    unittest.main()
