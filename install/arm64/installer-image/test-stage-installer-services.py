#!/usr/bin/python3
"""Focused tests for offline installer service staging."""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "stage-installer-services.py"
SPEC = importlib.util.spec_from_file_location("stage_installer_services", MODULE_PATH)
assert SPEC and SPEC.loader
stage = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = stage
SPEC.loader.exec_module(stage)


class StageInstallerServicesTests(unittest.TestCase):
    def make_binary(self, directory: Path) -> tuple[Path, str]:
        payload = bytearray(128)
        payload[:4] = b"\x7fELF"
        payload[4] = 2
        payload[5] = 1
        struct.pack_into("<H", payload, 18, stage.ELF_MACHINE_AARCH64)
        path = directory / "hypr-rdp"
        path.write_bytes(payload)
        path.chmod(0o755)
        return path, hashlib.sha256(payload).hexdigest()

    def make_target(self, directory: Path) -> Path:
        target = directory / "rootfs"
        for relative in (
            "boot",
            "etc/systemd/system/multi-user.target.wants",
            "etc/systemd/system/sockets.target.wants",
            "etc/systemd/system/network-online.target.wants",
            "etc/systemd/system/network.target.wants",
            "usr/lib/systemd/system",
        ):
            destination = target / relative
            destination.mkdir(parents=True, exist_ok=True)
            current = destination
            while current != target:
                current.chmod(0o755)
                current = current.parent
        for unit in ("NetworkManager.service", "sshd.service"):
            (target / "usr/lib/systemd/system" / unit).write_text("[Unit]\n", encoding="utf-8")
        (target / "etc/systemd/system/multi-user.target.wants/systemd-networkd.service").symlink_to(
            "/usr/lib/systemd/system/systemd-networkd.service"
        )
        (target / "etc/systemd/system/sockets.target.wants/systemd-networkd.socket").symlink_to(
            "/usr/lib/systemd/system/systemd-networkd.socket"
        )
        (target / "etc/systemd/system/network-online.target.wants/systemd-networkd-wait-online.service").symlink_to(
            "/usr/lib/systemd/system/systemd-networkd-wait-online.service"
        )
        (target / "etc/systemd/system/dbus-org.freedesktop.network1.service").symlink_to(
            "/usr/lib/systemd/system/systemd-networkd.service"
        )
        for unit in (
            "systemd-networkd-resolve-hook.socket",
            "systemd-networkd-varlink-metrics.socket",
            "systemd-networkd-varlink.socket",
        ):
            (target / "etc/systemd/system/sockets.target.wants" / unit).symlink_to(
                "/usr/lib/systemd/system/" + unit
            )
        return target

    def test_stages_verified_artifacts_and_explicit_enablement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.make_target(root)
            binary, digest = self.make_binary(root)
            result = stage.stage_services(target, binary, digest, require_root=False)
            self.assertEqual(result.binary_sha256, digest)
            self.assertEqual(result.dynamic_dependency_check, "checked 0 NEEDED entries")
            self.assertEqual((target / stage.BUILDER_MARKER).read_bytes(), stage.BUILDER_MARKER_CONTENT)
            self.assertEqual(stat.S_IMODE((target / stage.BUILDER_MARKER).stat().st_mode), 0o644)
            self.assertTrue((target / "boot/installer-settings.example.toml").is_file())
            self.assertFalse((target / "boot/installer-settings.toml").exists())
            self.assertEqual((target / "usr/share/omarchy-pi/hypr-rdp.sha256").read_text(), digest + "\n")
            self.assertTrue((target / "usr/bin/hypr-rdp").is_file())
            self.assertFalse((target / "etc/systemd/system/multi-user.target.wants/systemd-networkd.service").exists())
            self.assertFalse((target / "etc/systemd/system/sockets.target.wants/systemd-networkd.socket").exists())
            self.assertFalse((target / "etc/systemd/system/dbus-org.freedesktop.network1.service").exists())
            for unit in (
                "systemd-networkd-resolve-hook.socket",
                "systemd-networkd-varlink-metrics.socket",
                "systemd-networkd-varlink.socket",
            ):
                self.assertFalse((target / "etc/systemd/system/sockets.target.wants" / unit).exists())
            wants = target / "etc/systemd/system/multi-user.target.wants"
            self.assertEqual(os.readlink(wants / "NetworkManager.service"), "/usr/lib/systemd/system/NetworkManager.service")
            self.assertEqual(os.readlink(wants / "sshd.service"), "/usr/lib/systemd/system/sshd.service")
            self.assertEqual(os.readlink(wants / "omarchy-installer-launch.service"), "/etc/systemd/system/omarchy-installer-launch.service")
            self.assertFalse((wants / "omarchy-installer-session@.service").exists())
            user_link = target / "etc/systemd/user/graphical-session.target.wants/omarchy-installer-rdp.service"
            self.assertEqual(os.readlink(user_link), "../omarchy-installer-rdp.service")

            second = stage.stage_services(target, binary, digest, require_root=False)
            self.assertEqual(second.installed_files, ())
            self.assertEqual(second.enabled_links, ())

    def test_hash_mismatch_and_private_settings_fail_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.make_target(root)
            binary, digest = self.make_binary(root)
            with self.assertRaisesRegex(stage.ServiceStageError, "SHA-256"):
                stage.stage_services(target, binary, "0" * 64, require_root=False)
            self.assertFalse((target / "usr/local").exists())
            (target / "boot/installer-settings.toml").write_text("[private]\npassword = \"secret\"\n", encoding="utf-8")
            with self.assertRaisesRegex(stage.ServiceStageError, "private"):
                stage.stage_services(target, binary, digest, require_root=False)
            self.assertFalse((target / "usr/local").exists())

    def test_non_aarch64_binary_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.make_target(root)
            binary = root / "x86"
            payload = bytearray(64)
            payload[:4] = b"\x7fELF"
            payload[4] = 2
            payload[5] = 1
            struct.pack_into("<H", payload, 18, 62)
            binary.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            with self.assertRaisesRegex(stage.ServiceStageError, "aarch64"):
                stage.stage_services(target, binary, digest, require_root=False)


if __name__ == "__main__":
    unittest.main()
