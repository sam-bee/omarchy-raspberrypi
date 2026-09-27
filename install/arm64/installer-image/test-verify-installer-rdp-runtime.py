#!/usr/bin/python3
"""Focused tests for the installer direct-LAN RDP preflight."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "verify-installer-rdp-runtime.py"
SPEC = importlib.util.spec_from_file_location("verify_installer_rdp_runtime", MODULE_PATH)
assert SPEC and SPEC.loader
runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime
SPEC.loader.exec_module(runtime)


class VerifyInstallerRdpRuntimeTests(unittest.TestCase):
    def make_home(self) -> tuple[tempfile.TemporaryDirectory[str], Path, int]:
        temporary = tempfile.TemporaryDirectory()
        home = Path(temporary.name) / "home"
        home.mkdir(mode=0o755)
        config = home / ".config" / runtime.PROFILE_DIRECTORY_NAME
        config.mkdir(parents=True, mode=0o700)
        config.chmod(0o700)
        password_path = config / "password"
        expected = (
            'bind = "0.0.0.0:3389"\n'
            'username = "installer"\n'
            f'password_file = "{password_path}"\n'
            'resolution = "1280x720"\n'
            'fps = 20\n'
            'egfx_codec = "avc420"\n'
            'audio_mode = "off"\n'
            'file_transfer_mode = "off"\n'
        ).encode("utf-8")
        (config / "config.toml").write_bytes(expected)
        password_path.write_bytes(b"rdp-secret")
        for path in (config / "config.toml", password_path):
            path.chmod(0o600)
        return temporary, home, os.getuid()

    def test_authenticated_direct_profile_is_accepted(self) -> None:
        temporary, home, uid = self.make_home()
        self.addCleanup(temporary.cleanup)
        config_dir, password = runtime.check_config(home, uid, "installer")
        self.assertEqual(config_dir, home / ".config/omarchy-installer-rdp")
        self.assertEqual(password.read_bytes(), b"rdp-secret")
        tls = home / ".config" / runtime.TLS_DIRECTORY_NAME
        tls.mkdir(mode=0o700)
        self.assertFalse(runtime.check_tls(home, uid))

    def test_wrong_bind_and_plaintext_password_are_rejected(self) -> None:
        temporary, home, uid = self.make_home()
        self.addCleanup(temporary.cleanup)
        config = home / ".config/omarchy-installer-rdp/config.toml"
        config.write_text(config.read_text(encoding="utf-8").replace("0.0.0.0:3389", "127.0.0.1:3389"), encoding="utf-8")
        with self.assertRaises(SystemExit):
            runtime.check_config(home, uid, "installer")
        config.write_text(
            config.read_text(encoding="utf-8").replace('bind = "127.0.0.1:3389"\n', 'bind = "0.0.0.0:3389"\npassword = "secret"\n'),
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit):
            runtime.check_config(home, uid, "installer")

    def test_tls_temporary_or_partial_state_is_rejected(self) -> None:
        temporary, home, uid = self.make_home()
        self.addCleanup(temporary.cleanup)
        tls = home / ".config" / runtime.TLS_DIRECTORY_NAME
        tls.mkdir(mode=0o700)
        (tls / ".key.pem.tmp").write_bytes(b"partial")
        (tls / ".key.pem.tmp").chmod(0o600)
        with self.assertRaises(SystemExit):
            runtime.check_tls(home, uid)
        (tls / ".key.pem.tmp").unlink()
        (tls / "key.pem").write_bytes(b"partial")
        (tls / "key.pem").chmod(0o600)
        with self.assertRaises(SystemExit):
            runtime.check_tls(home, uid)

    def test_non_private_profile_file_is_rejected(self) -> None:
        temporary, home, uid = self.make_home()
        self.addCleanup(temporary.cleanup)
        password = home / ".config/omarchy-installer-rdp/password"
        password.chmod(0o644)
        with self.assertRaises(SystemExit):
            runtime.check_config(home, uid, "installer")


if __name__ == "__main__":
    unittest.main()
