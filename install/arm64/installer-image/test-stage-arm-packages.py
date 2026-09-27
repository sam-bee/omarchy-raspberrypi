#!/usr/bin/python3
"""Focused mocked-command tests for stage-arm-packages.py."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("stage-arm-packages.py")
SPEC = importlib.util.spec_from_file_location("stage_arm_packages", MODULE_PATH)
assert SPEC and SPEC.loader
stage = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = stage
SPEC.loader.exec_module(stage)


class StageArmPackagesTests(unittest.TestCase):
    def make_rootfs(self, directory: Path, *, gpgdir: bool = True) -> tuple[Path, Path]:
        rootfs = directory / "rootfs"
        keyrings = rootfs / "usr/share/pacman/keyrings"
        keyrings.mkdir(parents=True)
        (rootfs / "usr/bin").mkdir(parents=True)
        (rootfs / "usr/bin/pacman-key").write_bytes(b"#!/bin/sh\n")
        (rootfs / "usr/bin/pacman-key").chmod(0o755)
        (rootfs / "etc/pacman.d").mkdir(parents=True)
        for name in ("archlinux.gpg", "archlinuxarm.gpg", "archlinux-trusted", "archlinuxarm-trusted"):
            (keyrings / name).write_bytes(b"keyring fixture")
        if gpgdir:
            (rootfs / "etc/pacman.d/gnupg").mkdir(parents=True)
        (rootfs / "boot").mkdir()
        (rootfs / "var/lib/pacman").mkdir(parents=True)
        (rootfs / "etc/pacman.conf").write_text("Include=/etc/pacman.d/mirrorlist\n", encoding="utf-8")
        if gpgdir:
            (rootfs / "etc/pacman.d/gnupg/pubring.gpg").write_bytes(b"trusted keyring")
        manifest = directory / "rootfs-manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "staging": {"rootfs_directory": "rootfs"},
                    "source": {
                        "archive_sha256": "a" * 64,
                        "signer_fingerprint": "B" * 40,
                    },
                }
            ),
            encoding="utf-8",
        )
        return rootfs, manifest

    def fake_pacman(self, calls: list[list[str]], *, generic_remaining: bool = False):
        def run(command, **kwargs):
            command = list(command)
            calls.append(command)
            if "--print" in command:
                output = "\n".join(
                    f"{name}\t1.0-1\tcore" for name in stage.TRANSACTION_TARGETS
                ) + "\n"
                return subprocess.CompletedProcess(command, 0, stdout=output.encode(), stderr=b"")
            if "--query" in command:
                names = list(stage.TRANSACTION_TARGETS)
                if generic_remaining:
                    names.extend(stage.GENERIC_BOOT_PACKAGES)
                output = ("\n".join(names) + "\n").encode()
                return subprocess.CompletedProcess(command, 0, stdout=output, stderr=b"")
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

        return run

    def test_preview_uses_scratch_paths_and_writes_only_external_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rootfs, rootfs_manifest = self.make_rootfs(directory)
            package_manifest = directory / "packages.json"
            calls: list[list[str]] = []
            configs: list[str] = []
            fake = self.fake_pacman(calls)

            def run(command, **kwargs):
                if "--config" in command:
                    config = Path(command[command.index("--config") + 1])
                    configs.append(config.read_text(encoding="utf-8"))
                return fake(command, **kwargs)

            with patch.object(stage.subprocess, "run", side_effect=run):
                manifest = stage.stage_packages(
                    rootfs,
                    rootfs_manifest,
                    package_manifest,
                    require_native_aarch64=False,
                    require_root=False,
                )
            self.assertEqual(manifest["mode"], "preview")
            self.assertEqual(manifest["transaction"]["runtime_roots"], list(stage.RUNTIME_ROOTS))
            self.assertIn("pipewire", manifest["transaction"]["runtime_roots"])
            self.assertIn("iw", manifest["transaction"]["runtime_roots"])
            self.assertNotIn("hypr-rdp", json.dumps(manifest))
            self.assertTrue(package_manifest.is_file())
            preview = next(command for command in calls if "--print" in command)
            self.assertIn("--root", preview)
            self.assertEqual(preview[preview.index("--root") + 1], str(rootfs.resolve()))
            self.assertNotEqual(preview[preview.index("--dbpath") + 1], str(rootfs / "var/lib/pacman"))
            self.assertNotEqual(preview[preview.index("--cachedir") + 1], str(rootfs / "var/cache/pacman/pkg"))
            self.assertIn("--gpgdir", preview)
            self.assertNotEqual(preview[preview.index("--gpgdir") + 1], str(rootfs / "etc/pacman.d/gnupg"))
            self.assertIn("--sysupgrade", preview)
            self.assertTrue(configs)
            self.assertIn("Architecture = aarch64", configs[0])
            self.assertIn("SigLevel = Required", configs[0])
            self.assertIn("DatabaseOptional", configs[0])
            self.assertIn("Server = https://mirror.archlinuxarm.org/$arch/$repo", configs[0])
            self.assertIn("[alarm]", configs[0])
            self.assertNotIn("Include=", configs[0])
            self.assertFalse((rootfs / "var/cache").exists())
            self.assertFalse((rootfs / "var/log").exists())
            self.assertFalse((rootfs / "etc/pacman.d/hooks").exists())

    def test_apply_uses_explicit_target_paths_and_checks_replacements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rootfs, rootfs_manifest = self.make_rootfs(directory)
            package_manifest = directory / "packages.json"
            calls: list[list[str]] = []
            with patch.object(stage.subprocess, "run", side_effect=self.fake_pacman(calls)):
                manifest = stage.stage_packages(
                    rootfs,
                    rootfs_manifest,
                    package_manifest,
                    apply=True,
                    require_native_aarch64=False,
                    require_root=False,
                )
            self.assertEqual(manifest["mode"], "apply")
            preview_call = next(
                command for command in calls if "--sync" in command and "--print" in command
            )
            self.assertNotEqual(preview_call[0], "systemd-nspawn")
            self.assertNotEqual(preview_call[preview_call.index("--dbpath") + 1], str(rootfs / "var/lib/pacman"))
            apply_call = next(
                command for command in calls if "--sync" in command and "--print" not in command
            )
            preflight_call = next(command for command in calls if any("getent" in item for item in command))
            self.assertEqual(preflight_call[0], "systemd-nspawn")
            self.assertIn("--network-namespace-path=/proc/1/ns/net", preflight_call)
            self.assertIn("--resolv-conf=replace-host", preflight_call)
            self.assertIn("ahostsv4", preflight_call)
            self.assertEqual(preflight_call[-1], "mirror.archlinuxarm.org")
            self.assertEqual(apply_call[0], "systemd-nspawn")
            self.assertIn("--register=no", apply_call)
            self.assertIn("--private-users=no", apply_call)
            self.assertIn("--network-namespace-path=/proc/1/ns/net", apply_call)
            self.assertIn("--resolv-conf=replace-host", apply_call)
            self.assertIn("--directory", apply_call)
            self.assertEqual(apply_call[apply_call.index("--directory") + 1], str(rootfs.resolve()))
            self.assertIn("--root", apply_call)
            self.assertEqual(apply_call[apply_call.index("--root") + 1], "/")
            self.assertEqual(apply_call[apply_call.index("--dbpath") + 1], "/var/lib/pacman")
            self.assertEqual(apply_call[apply_call.index("--cachedir") + 1], "/var/cache/pacman/pkg")
            self.assertEqual(apply_call[apply_call.index("--logfile") + 1], "/var/log/pacman.log")
            self.assertEqual(apply_call[apply_call.index("--gpgdir") + 1], "/etc/pacman.d/gnupg")
            self.assertIn("linux-rpi", apply_call)
            self.assertIn("raspberrypi-bootloader", apply_call)
            self.assertIn("--sysupgrade", apply_call)
            self.assertNotIn("linux-aarch64", apply_call)
            self.assertNotIn("uboot-raspberrypi", apply_call)
            self.assertTrue((rootfs / "var/cache/pacman/pkg").is_dir())
            self.assertTrue((rootfs / "etc/pacman.d/hooks").is_dir())
            self.assertIn("installed_requested_packages", manifest["transaction"])
            keyring_cleanup = manifest["repositories"]["keyring"]["post_transaction_cleanup"]
            self.assertEqual(keyring_cleanup["agents_stopped"], ["gpg-agent", "scdaemon"])
            self.assertTrue(keyring_cleanup["special_files_verified"])
            self.assertTrue(manifest["repositories"]["keyring"]["release_sanitization"]["generated_pacman_master_key_retained"])

    def test_apply_network_preflight_failure_leaves_target_unprepared(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rootfs, rootfs_manifest = self.make_rootfs(directory)
            package_manifest = directory / "packages.json"
            calls: list[list[str]] = []
            fake = self.fake_pacman(calls)

            def fail_network(command, **kwargs):
                command = list(command)
                calls.append(command)
                if any("getent" in item for item in command):
                    return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"resolver failed")
                return fake(command, **kwargs)

            with patch.object(stage.subprocess, "run", side_effect=fail_network):
                with self.assertRaisesRegex(stage.PackageStageError, "nspawn network preflight failed"):
                    stage.stage_packages(
                        rootfs,
                        rootfs_manifest,
                        package_manifest,
                        apply=True,
                        require_native_aarch64=False,
                        require_root=False,
                    )
            self.assertFalse((rootfs / "var/cache").exists())
            self.assertFalse((rootfs / "var/log").exists())
            self.assertFalse((rootfs / "etc/pacman.d/hooks").exists())
            self.assertFalse(package_manifest.exists())

    def test_apply_preview_failure_does_not_prepare_target_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rootfs, rootfs_manifest = self.make_rootfs(directory)
            package_manifest = directory / "packages.json"
            calls: list[list[str]] = []
            fake = self.fake_pacman(calls)

            def fail_preview(command, **kwargs):
                if "--sync" in command and "--print" in command:
                    calls.append(list(command))
                    return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"conflict")
                return fake(command, **kwargs)

            with patch.object(stage.subprocess, "run", side_effect=fail_preview):
                with self.assertRaisesRegex(stage.PackageStageError, "pacman transaction failed"):
                    stage.stage_packages(
                        rootfs,
                        rootfs_manifest,
                        package_manifest,
                        apply=True,
                        require_native_aarch64=False,
                        require_root=False,
                    )
            self.assertFalse((rootfs / "var/cache").exists())
            self.assertFalse((rootfs / "var/log").exists())
            self.assertFalse((rootfs / "etc/pacman.d/hooks").exists())

    def test_preview_bootstraps_scratch_gpgdir_with_pacman_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rootfs, rootfs_manifest = self.make_rootfs(directory, gpgdir=False)
            package_manifest = directory / "packages.json"
            calls: list[list[str]] = []
            with patch.object(stage.subprocess, "run", side_effect=self.fake_pacman(calls)):
                manifest = stage.stage_packages(
                    rootfs,
                    rootfs_manifest,
                    package_manifest,
                    require_native_aarch64=False,
                    require_root=False,
                )
            key_calls = [command for command in calls if "/usr/bin/pacman-key" in command]
            self.assertEqual(len(key_calls), 2)
            self.assertIn("--init", key_calls[0])
            self.assertIn("--populate", key_calls[1])
            self.assertIn("archlinuxarm", key_calls[1])
            self.assertEqual(
                manifest["repositories"]["keyring"]["bootstrapped_from_target_files"],
                [
                    "usr/share/pacman/keyrings/archlinux.gpg",
                    "usr/share/pacman/keyrings/archlinuxarm.gpg",
                    "usr/share/pacman/keyrings/archlinux-trusted",
                    "usr/share/pacman/keyrings/archlinuxarm-trusted",
                ],
            )
            self.assertFalse((rootfs / "etc/pacman.d/gnupg").exists())

    def test_apply_rejects_a_remaining_generic_boot_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rootfs, rootfs_manifest = self.make_rootfs(directory)
            package_manifest = directory / "packages.json"
            calls: list[list[str]] = []
            with patch.object(
                stage.subprocess,
                "run",
                side_effect=self.fake_pacman(calls, generic_remaining=True),
            ):
                with self.assertRaisesRegex(stage.PackageStageError, "generic boot packages remain"):
                    stage.stage_packages(
                        rootfs,
                        rootfs_manifest,
                        package_manifest,
                        apply=True,
                        require_native_aarch64=False,
                        require_root=False,
                    )
            remove_calls = [command for command in calls if "--remove" in command]
            self.assertTrue(remove_calls)
            self.assertIn("uboot-raspberrypi", remove_calls[0])
            self.assertIn("linux-aarch64", remove_calls[0])
            self.assertIn("--dbonly", remove_calls[0])
            self.assertEqual(remove_calls[-1][0], "systemd-nspawn")
            self.assertFalse(package_manifest.exists())

    def test_preview_removes_generic_records_only_in_scratch_db(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rootfs, rootfs_manifest = self.make_rootfs(directory)
            package_manifest = directory / "packages.json"
            calls: list[list[str]] = []
            with patch.object(
                stage.subprocess,
                "run",
                side_effect=self.fake_pacman(calls, generic_remaining=True),
            ):
                stage.stage_packages(
                    rootfs,
                    rootfs_manifest,
                    package_manifest,
                    require_native_aarch64=False,
                    require_root=False,
                )
            remove_calls = [command for command in calls if "--remove" in command]
            self.assertTrue(remove_calls)
            self.assertIn("--dbonly", remove_calls[0])
            self.assertNotIn("--print", remove_calls[0])

    def test_guards_reject_live_root_untrusted_repo_and_in_root_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with self.assertRaises(stage.PackageStageError):
                stage._require_target_root(Path("/"))
            with self.assertRaises(stage.PackageStageError):
                stage._validate_repo_server("file:///var/cache/pacman/pkg/$arch/$repo")
            rootfs, rootfs_manifest = self.make_rootfs(directory)
            with self.assertRaisesRegex(stage.PackageStageError, "outside the target"):
                stage.stage_packages(
                    rootfs,
                    rootfs_manifest,
                    rootfs / "package-manifest.json",
                    require_native_aarch64=False,
                    require_root=False,
                )

    def test_boot_tree_guard_rejects_symlink_after_package_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            boot = Path(temporary) / "boot"
            boot.mkdir()
            (boot / "kernel8.img").write_bytes(b"kernel")
            (boot / "kernel-link").symlink_to("kernel8.img")
            with self.assertRaisesRegex(stage.PackageStageError, "symlink unsupported by FAT"):
                stage._validate_fat_boot_tree(boot)

    def test_gpg_cleanup_stops_only_target_agents_and_removes_sockets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gpgdir = Path(temporary) / "gnupg"
            gpgdir.mkdir()
            (gpgdir / "pubring.kbx").write_bytes(b"public keyring")
            private_key = gpgdir / "private-keys-v1.d"
            private_key.mkdir()
            (private_key / "master.key").write_bytes(b"private key retained for step 2")
            socket_path = gpgdir / "S.gpg-agent"
            socket_path.write_bytes(b"mock agent socket")
            real_lstat = Path.lstat

            def pretend_socket(path: Path):
                info = real_lstat(path)
                if path == socket_path:
                    return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o700)
                return info

            with patch.object(stage.Path, "lstat", pretend_socket):
                with patch.object(
                    stage.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""),
                ) as run:
                    removed = stage._cleanup_target_gpg_sockets(gpgdir)
            self.assertEqual(removed, ["S.gpg-agent"])
            self.assertFalse(socket_path.exists())
            self.assertTrue((gpgdir / "pubring.kbx").is_file())
            self.assertTrue((private_key / "master.key").is_file())
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(
                commands,
                [
                    ["gpgconf", "--homedir", str(gpgdir.resolve()), "--kill", "gpg-agent"],
                    ["gpgconf", "--homedir", str(gpgdir.resolve()), "--kill", "scdaemon"],
                ],
            )

    def test_gpg_cleanup_rejects_non_socket_special_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gpgdir = Path(temporary) / "gnupg"
            gpgdir.mkdir()
            fifo = gpgdir / "unexpected.fifo"
            os.mkfifo(fifo, 0o600)
            with patch.object(
                stage.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""),
            ):
                with self.assertRaisesRegex(stage.PackageStageError, "unsupported special files"):
                    stage._cleanup_target_gpg_sockets(gpgdir)
            self.assertTrue(fifo.exists())


if __name__ == "__main__":
    unittest.main()
