#!/usr/bin/python3
"""Focused tests for the bounded FAT-side installer settings parser."""

from __future__ import annotations

import base64
import io
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout


HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from settings import (  # noqa: E402
    InstallerSettings,
    SettingsError,
    load_settings,
    parse_settings,
)


PUBLIC_KEY = "ssh-ed25519 " + base64.b64encode(b"x" * 32).decode("ascii")


def document(*, wifi: str = "", ssh: str | None = None) -> str:
    ssh_block = ssh if ssh is not None else f'authorized_key = "{PUBLIC_KEY}"'
    return (
        '[installer]\n'
        'hostname = "pi-installer"\n'
        'username = "sierra"\n'
        f'{wifi}'
        '[ssh]\n'
        f'{ssh_block}\n'
        '\n[rdp]\n'
        'password = "separate-rdp-pass"\n'
    )


class SettingsTests(unittest.TestCase):
    def test_valid_ethernet_only_result_is_typed(self) -> None:
        result = parse_settings(document())
        self.assertIsInstance(result, InstallerSettings)
        self.assertEqual(result.hostname, "pi-installer")
        self.assertEqual(result.username, "sierra")
        self.assertIsNone(result.wifi)
        self.assertEqual(result.ssh.authorized_key, PUBLIC_KEY)
        self.assertIsNone(result.ssh.password)
        self.assertEqual(result.rdp.password, "separate-rdp-pass")

    def test_valid_wifi_and_password_auth(self) -> None:
        result = parse_settings(
            document(
                wifi='''[wifi]
country = "GB"
ssid = "test-network"
password = "correct horse battery"
''',
                ssh='password = "login-password"',
            )
        )
        self.assertEqual(result.wifi.country, "GB")
        self.assertEqual(result.wifi.ssid, "test-network")
        self.assertEqual(result.ssh.password, "login-password")
        self.assertIsNone(result.ssh.authorized_key)

    def test_example_contains_no_active_secret_values(self) -> None:
        example = (HERE / "installer-settings.example.toml").read_text()
        parsed_lines = [
            line.strip()
            for line in example.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertTrue(any(line.startswith("[installer]") for line in parsed_lines))
        self.assertFalse(any("password" in line or "authorized_key" in line for line in parsed_lines))

    def test_unknown_and_duplicate_keys_are_rejected(self) -> None:
        with self.assertRaises(SettingsError):
            parse_settings(document().replace("[installer]", "[installer]\nextra = true"))
        with self.assertRaises(SettingsError):
            parse_settings(document().replace('username = "sierra"', 'username = "sierra"\nusername = "other"'))

    def test_invalid_lengths_names_and_authentication_are_rejected(self) -> None:
        cases = (
            document().replace('hostname = "pi-installer"', 'hostname = "-bad"'),
            document().replace('hostname = "pi-installer"', 'hostname = "pi-installer.local"'),
            document().replace('hostname = "pi-installer"', f'hostname = "{"a" * 64}"'),
            document().replace('username = "sierra"', 'username = "BadUser"'),
            document().replace('username = "sierra"', 'username = "root"'),
            document().replace('username = "sierra"', 'username = "alarm"'),
            document().replace('username = "sierra"', 'username = "nobody"'),
            document().replace(f'authorized_key = "{PUBLIC_KEY}"', 'authorized_key = "not-a-key"'),
            document().replace('password = "separate-rdp-pass"', 'password = "short"'),
            document(ssh='authorized_key = ""'),
        )
        for invalid in cases:
            with self.subTest(invalid=invalid[:40]), self.assertRaises(SettingsError):
                parse_settings(invalid)

    def test_control_characters_are_rejected_without_echoing_values(self) -> None:
        secret = "super-secret-value"
        invalid = document().replace('hostname = "pi-installer"', 'hostname = "pi\\u0001"')
        invalid = invalid.replace('separate-rdp-pass', secret)
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            with self.assertRaises(SettingsError) as context:
                parse_settings(invalid)
        self.assertNotIn(secret, str(context.exception))
        self.assertNotIn(secret, output.getvalue())

    def test_secret_fields_are_hidden_from_repr(self) -> None:
        result = parse_settings(document())
        rendered = repr(result)
        self.assertNotIn("separate-rdp-pass", rendered)
        self.assertNotIn(PUBLIC_KEY, rendered)

    def test_load_settings_does_not_leak_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "installer-settings.toml"
            path.write_text(document())
            self.assertEqual(load_settings(path).username, "sierra")
            path.write_text("[installer]\nhostname = \"bad\"\n")
            with self.assertRaises(SettingsError) as context:
                load_settings(path)
            self.assertNotIn("bad", str(context.exception))

    def test_document_size_and_non_toml_are_rejected(self) -> None:
        with self.assertRaises(SettingsError):
            parse_settings("#" * (64 * 1024 + 1))
        with self.assertRaises(SettingsError):
            parse_settings("this is not = valid TOML")


if __name__ == "__main__":
    unittest.main()
