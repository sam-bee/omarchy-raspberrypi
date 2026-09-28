#!/usr/bin/python3
"""Focused tests for offline installer service staging."""

from __future__ import annotations

import hashlib
import json
import importlib.util
import os
from pathlib import Path
import stat
import struct
import subprocess
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
            "usr/lib/systemd/system-preset",
        ):
            destination = target / relative
            destination.mkdir(parents=True, exist_ok=True)
            current = destination
            while current != target:
                current.chmod(0o755)
                current = current.parent
        for unit in ("NetworkManager.service", "sshd.service"):
            (target / "usr/lib/systemd/system" / unit).write_text("[Unit]\n", encoding="utf-8")
        (target / "usr/lib/systemd/system/systemd-networkd.service").write_text(
            "[Unit]\n[Install]\nWantedBy=multi-user.target\n",
            encoding="utf-8",
        )
        (target / "usr/lib/systemd/system/systemd-networkd.socket").write_text(
            "[Unit]\n[Install]\nWantedBy=sockets.target\n",
            encoding="utf-8",
        )
        (target / "usr/lib/systemd/system/systemd-networkd-wait-online.service").write_text(
            "[Unit]\n[Install]\nWantedBy=network-online.target\n",
            encoding="utf-8",
        )
        (target / "usr/lib/systemd/system-preset/90-systemd.preset").write_text(
            "enable systemd-networkd.service\n"
            "enable systemd-networkd.socket\n"
            "enable systemd-networkd-wait-online.service\n",
            encoding="utf-8",
        )
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

    def test_provenance_records_exact_runtime_hashes_and_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = self.make_target(directory)
            binary, digest = self.make_binary(directory)
            result = stage.stage_services(target, binary, digest, require_root=False, source_revision="a" * 40)
            data = json.loads((target / "usr/lib/omarchy-pi/installer-provenance.json").read_text())
            self.assertEqual(data["source_revision"], "a" * 40)
            self.assertEqual(data, result.installer_provenance)
            for relative, expected in data["files"].items():
                self.assertEqual(hashlib.sha256((target / relative).read_bytes()).hexdigest(), expected)
            actual = hashlib.sha256(json.dumps(data["files"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            self.assertEqual(data["runtime_sha256"], actual)

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
            self.assertTrue((target / "etc/systemd/system/omarchy-pi-install.service").is_file())
            self.assertFalse(os.path.lexists(target / "etc/systemd/system/multi-user.target.wants/omarchy-pi-install.service"))
            preset = target / stage.NETWORKD_PRESET
            self.assertEqual(preset.read_bytes(), stage.NETWORKD_PRESET_CONTENT)
            self.assertEqual(stat.S_IMODE(preset.stat().st_mode), 0o644)
            self.assertFalse((target / "etc/systemd/system/multi-user.target.wants/systemd-networkd.service").is_symlink())
            self.assertFalse((target / "etc/systemd/system/sockets.target.wants/systemd-networkd.socket").is_symlink())
            self.assertFalse((target / "etc/systemd/system/dbus-org.freedesktop.network1.service").is_symlink())
            for unit in (
                "systemd-networkd-resolve-hook.socket",
                "systemd-networkd-varlink-metrics.socket",
                "systemd-networkd-varlink.socket",
            ):
                self.assertFalse((target / "etc/systemd/system/sockets.target.wants" / unit).is_symlink())
            preset.unlink()
            preset_result = subprocess.run(
                [
                    "systemctl",
                    f"--root={target}",
                    "preset",
                    "systemd-networkd.service",
                    "systemd-networkd.socket",
                    "systemd-networkd-wait-online.service",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(preset_result.returncode, 0, preset_result.stderr)
            vendor_links = (
                "etc/systemd/system/multi-user.target.wants/systemd-networkd.service",
                "etc/systemd/system/sockets.target.wants/systemd-networkd.socket",
                "etc/systemd/system/network-online.target.wants/systemd-networkd-wait-online.service",
            )
            for relative in vendor_links:
                self.assertTrue((target / relative).is_symlink())

            preset.write_bytes(stage.NETWORKD_PRESET_CONTENT)
            preset.chmod(0o644)
            preset_result = subprocess.run(
                [
                    "systemctl",
                    f"--root={target}",
                    "preset",
                    "systemd-networkd.service",
                    "systemd-networkd.socket",
                    "systemd-networkd-wait-online.service",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(preset_result.returncode, 0, preset_result.stderr)
            for relative in vendor_links:
                self.assertFalse((target / relative).is_symlink())
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

    def test_public_payload_directories_are_traversable_under_restrictive_umask(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.make_target(root)
            binary, digest = self.make_binary(root)
            preexisting_public = target / "usr/local"
            preexisting_public.mkdir(parents=True)
            preexisting_public.chmod(0o700)
            previous_umask = os.umask(0o077)
            try:
                stage.stage_services(target, binary, digest, require_root=False)
            finally:
                os.umask(previous_umask)

            for relative in stage.PUBLIC_PAYLOAD_DIRECTORIES:
                path = target / relative
                self.assertTrue(path.is_dir(), relative)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755, relative)
            for relative, mode in stage.LIBEXEC_FILES.items():
                path = target / "usr/local/libexec/omarchy-pi" / relative
                self.assertTrue(path.is_file(), relative)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode, relative)

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

    def test_vendor_networkd_enablement_fails_closed_without_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.make_target(root)
            vendor_wants = target / "usr/lib/systemd/system/multi-user.target.wants"
            vendor_wants.mkdir(parents=True)
            vendor_link = vendor_wants / "systemd-networkd.service"
            vendor_link.symlink_to("/usr/lib/systemd/system/systemd-networkd.service")
            binary, digest = self.make_binary(root)
            with self.assertRaisesRegex(stage.ServiceStageError, "networkd enablement remains"):
                stage.stage_services(target, binary, digest, require_root=False)
            self.assertTrue(vendor_link.is_symlink())


if __name__ == "__main__":
    unittest.main()
