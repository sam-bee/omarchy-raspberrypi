#!/usr/bin/python3
"""Focused offline tests for the ARM update helper boundaries."""

from __future__ import annotations

import hashlib
import gzip
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import update_lib  # noqa: E402
_migration_spec = importlib.util.spec_from_file_location("update_migrations", HERE / "update-migrations.py")
assert _migration_spec is not None and _migration_spec.loader is not None
update_migrations = importlib.util.module_from_spec(_migration_spec)
_migration_spec.loader.exec_module(update_migrations)
_preflight_spec = importlib.util.spec_from_file_location("update_preflight", HERE / "update-preflight.py")
assert _preflight_spec is not None and _preflight_spec.loader is not None
update_preflight = importlib.util.module_from_spec(_preflight_spec)
_preflight_spec.loader.exec_module(update_preflight)


class UpdateHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "root"
        self.home = Path(self.temp.name) / "home"
        self.source_old = Path(self.temp.name) / "old"
        self.source_new = Path(self.temp.name) / "new"
        for path in (self.root, self.home, self.source_old, self.source_new):
            path.mkdir()
        (self.root / "etc/mkinitcpio.conf.d").mkdir(parents=True)
        (self.root / "etc/mkinitcpio.d").mkdir(parents=True)
        (self.root / "etc/ssh").mkdir(parents=True)
        (self.root / "etc/pacman.d").mkdir(parents=True)
        (self.root / "etc/NetworkManager/system-connections").mkdir(parents=True)
        (self.root / "boot/overlays").mkdir(parents=True)
        (self.root / "usr/bin").mkdir(parents=True)
        (self.root / "usr/share/omarchy-pi").mkdir(parents=True)
        (self.home / ".config/omarchy-pi-rdp").mkdir(parents=True)
        (self.root / "etc/pacman.conf").write_text("SigLevel = Required DatabaseOptional\n")
        (self.root / "etc/passwd").write_text(
            f"root:x:0:0:root:/root:/bin/bash\n"
            f"tester:x:1000:1000:Tester:{self.home}:/bin/bash\n"
        )
        (self.root / "etc/shadow").write_text(
            "root:$6$root-hash:1:0:99999:7:::\n"
            "tester:$6$tester-hash:1:0:99999:7:::\n"
        )
        (self.root / "etc/mkinitcpio.conf").write_text(
            "MODULES=(nvme xhci_pci usb_storage uas usbhid hid_generic mmc_core mmc_block ext4)\n"
            "HOOKS=(base systemd modconf keyboard sd-vconsole block filesystems fsck)\n"
        )
        (self.root / "etc/mkinitcpio.d/linux-rpi.preset").write_text(
            "default_image='/boot/initramfs-linux.img'\n"
        )
        for name, content in {
            "config.txt": "dtparam=pciex1_gen=1\n",
            "cmdline.txt": "root=LABEL=root rw\n",
            "fstab": "LABEL=root / ext4 defaults 0 1\n",
            "crypttab": "",
            "sshd_config": "PermitRootLogin no\n",
            "mirrorlist": "Server = https://example.invalid/$arch\n",
        }.items():
            destination = self.root / ("boot/" + name if name in {"config.txt", "cmdline.txt"} else "etc/ssh/" + name if name == "sshd_config" else "etc/pacman.d/" + name if name == "mirrorlist" else "etc/" + name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)
        (self.root / "boot/kernel8.img").write_bytes(b"kernel")
        (self.root / "boot/initramfs-linux.img").write_bytes(b"initramfs")
        (self.root / "usr/bin/hypr-rdp").write_bytes(b"#!test\n")
        (self.root / "usr/bin/hypr-rdp").chmod(0o755)
        (self.root / "usr/share/omarchy-pi/hypr-rdp.sha256").write_text(hashlib.sha256(b"#!test\n").hexdigest() + "\n")
        (self.root / "usr/bin/ttfx").write_bytes(b"#!test\n")
        (self.root / "usr/bin/ttfx").chmod(0o755)
        package_db = Path(self.temp.name) / "packages.json"
        package_db.write_text(json.dumps({"hypr-rdp": "0.1.6-1", "ttfx": "0.3.2-1"}))
        self.previous_package_db = os.environ.get("OMARCHY_PI_PACKAGE_DB")
        self.previous_test = os.environ.get("OMARCHY_PI_TESTING")
        self.previous_allow_binary = os.environ.get("OMARCHY_PI_ALLOW_TEST_BINARY")
        os.environ["OMARCHY_PI_PACKAGE_DB"] = str(package_db)
        os.environ["OMARCHY_PI_TESTING"] = "1"
        os.environ["OMARCHY_PI_ALLOW_TEST_BINARY"] = "1"

    def tearDown(self) -> None:
        for name, value in (("OMARCHY_PI_PACKAGE_DB", self.previous_package_db), ("OMARCHY_PI_TESTING", self.previous_test), ("OMARCHY_PI_ALLOW_TEST_BINARY", self.previous_allow_binary)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temp.cleanup()

    def test_snapshot_includes_kernel8_and_reviewed_initramfs_contract(self) -> None:
        state = update_lib.snapshot(root=self.root, home=self.home)
        self.assertIn("kernel8.img", state["boot_entries"])
        self.assertIn("nvme", state["mkinitcpio"]["modules"])
        self.assertIn("sd-vconsole", state["mkinitcpio"]["hooks"])

    def test_protected_config_change_is_reported(self) -> None:
        before = update_lib.snapshot(root=self.root, home=self.home)
        (self.root / "boot/config.txt").write_text("dtparam=pciex1_gen=2\n")
        after = update_lib.snapshot(root=self.root, home=self.home)
        self.assertIn("/boot/config.txt", update_lib.compare_snapshots(before, after))

    def test_existing_authentication_state_change_is_reported_without_plaintext(self) -> None:
        before = update_lib.snapshot(root=self.root, home=self.home)
        (self.root / "etc/shadow").write_text(
            "root:$6$root-hash:1:0:99999:7:::\n"
            "tester:$6$changed-hash:1:0:99999:7:::\n"
        )
        after = update_lib.snapshot(root=self.root, home=self.home)
        self.assertTrue(any(item.startswith("account_auth:") for item in update_lib.changed_protected(before, after)))
        self.assertNotIn("tester-hash", json.dumps(before))

    def test_missing_kernel8_and_initramfs_fail_boot_verification(self) -> None:
        before = update_lib.snapshot(root=self.root, home=self.home)
        (self.root / "boot/kernel8.img").unlink()
        (self.root / "boot/initramfs-linux.img").unlink()
        after = update_lib.snapshot(root=self.root, home=self.home)
        failures = update_lib.verify_boot_state(before, after, root=self.root)
        self.assertTrue(any("kernel8.img" in failure for failure in failures))
        self.assertTrue(any("boot entry disappeared" in failure for failure in failures))

    def test_kernel_modules_and_initramfs_must_match_preset(self) -> None:
        version = "6.18.53-1-rpi"
        modules = self.root / "usr/lib/modules" / version
        modules.mkdir(parents=True)
        (modules / "pkgbase").write_text("linux-rpi\n")
        (modules / "modules.builtin").write_text("")
        (self.root / "etc/mkinitcpio.d/linux-rpi.preset").write_text(
            "ALL_kver='/usr/lib/modules/6.18.53-1-rpi'\n"
            "default_image='/boot/initramfs-linux.img'\n"
        )
        lsinitcpio = self.root / "usr/bin/lsinitcpio"
        lsinitcpio.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' usr/lib/modules/6.18.53-1-rpi/kernel/fs/ext4.ko\n"
        )
        lsinitcpio.chmod(0o755)
        package_record = self.root / "var/lib/pacman/local/linux-rpi-6.18.53-1"
        package_record.mkdir(parents=True)
        (package_record / "files").write_text("%FILES%\nboot/kernel8.img\n")
        mtree = (
            "./boot/kernel8.img type=file sha256digest="
            + hashlib.sha256((self.root / "boot/kernel8.img").read_bytes()).hexdigest()
            + "\n"
        )
        (package_record / "mtree").write_bytes(gzip.compress(mtree.encode()))
        state = update_lib.snapshot(root=self.root, home=self.home)
        self.assertEqual(update_lib.verify_kernel_artifacts(state, root=self.root), [])
        self.assertEqual(state["kernel_artifacts"]["linux_rpi_boot_files"]["kernel8.img"], True)
        (self.root / "etc/mkinitcpio.d/linux-rpi.preset").write_text(
            "ALL_kver='/usr/lib/modules/missing-kernel'\n"
            "default_image='/boot/initramfs-linux.img'\n"
        )
        broken = update_lib.snapshot(root=self.root, home=self.home)
        self.assertTrue(any("missing-kernel" in item for item in update_lib.verify_kernel_artifacts(broken, root=self.root)))

    def test_package_database_failure_does_not_look_like_empty_success(self) -> None:
        os.environ["OMARCHY_PI_PACKAGE_DB"] = str(self.root / "missing.json")
        with self.assertRaises(update_lib.UpdateCheckError):
            update_lib.package_db()

    def test_signature_policy_rejects_spacing_and_unsigned_package_levels(self) -> None:
        self.assertFalse(
            update_preflight.pacman_signature_failures(
                "SigLevel    = Required DatabaseOptional\nLocalFileSigLevel = Optional\n",
                root=str(self.root),
            )
        )
        self.assertTrue(
            update_preflight.pacman_signature_failures("SigLevel\t=\tNever\n", root=str(self.root))
        )
        self.assertTrue(
            update_preflight.pacman_signature_failures("SigLevel = PackageNever\n", root=str(self.root))
        )
        self.assertTrue(
            update_preflight.pacman_signature_failures("[core]\nSigLevel = PackageOptional\n", root=str(self.root))
        )

    def test_signature_policy_inherits_global_for_empty_and_partial_repo_overrides(self) -> None:
        def command_output(command: list[str], *, timeout: float = 15) -> tuple[int, str]:
            del timeout
            if command[-1] == "SigLevel" and "--repo" not in command:
                return 0, "PackageRequired\nPackageTrustedOnly\nDatabaseOptional\nDatabaseTrustedOnly\n"
            if command[-1] == "--repo-list":
                return 0, "core\nextra\n"
            repository = command[command.index("--repo") + 1]
            return 0, "" if repository == "core" else "DatabaseOptional\n"

        with (
            mock.patch.dict(os.environ, {"OMARCHY_PI_TESTING": "0"}),
            mock.patch.object(update_preflight.shutil, "which", return_value="/usr/bin/pacman-conf"),
            mock.patch.object(update_preflight, "command_output", side_effect=command_output),
        ):
            self.assertEqual(
                update_preflight.pacman_signature_failures(
                    "SigLevel = Required DatabaseOptional\n", root="/"
                ),
                [],
            )

    def test_signature_policy_rejects_hostile_repo_package_override(self) -> None:
        def command_output(command: list[str], *, timeout: float = 15) -> tuple[int, str]:
            del timeout
            if command[-1] == "SigLevel" and "--repo" not in command:
                return 0, "PackageRequired\nPackageTrustedOnly\nDatabaseOptional\n"
            if command[-1] == "--repo-list":
                return 0, "core\n"
            return 0, "PackageOptional\n"

        with (
            mock.patch.dict(os.environ, {"OMARCHY_PI_TESTING": "0"}),
            mock.patch.object(update_preflight.shutil, "which", return_value="/usr/bin/pacman-conf"),
            mock.patch.object(update_preflight, "command_output", side_effect=command_output),
        ):
            failures = update_preflight.pacman_signature_failures(
                "SigLevel = Required DatabaseOptional\n", root="/"
            )
        self.assertTrue(any("repository core" in failure for failure in failures))

    def test_migration_policy_requires_exact_hash_and_reason(self) -> None:
        old_migrations = self.source_old / "migrations"
        new_migrations = self.source_new / "migrations"
        old_migrations.mkdir()
        new_migrations.mkdir()
        (old_migrations / "100.sh").write_text("echo old\n")
        (new_migrations / "100.sh").write_text("echo changed\n")
        (new_migrations / "200.sh").write_text("echo new\n")
        policy = Path(self.temp.name) / "migrations.allowlist"
        policy.write_text(
            "100.sh\tskip\t" + hashlib.sha256((new_migrations / "100.sh").read_bytes()).hexdigest() + "\tPi-inapplicable test migration\n"
        )
        checked = update_migrations.check_migrations(self.source_old, self.source_new, policy)
        self.assertEqual(checked["changed"], ["100.sh"])
        self.assertTrue(checked["failures"])
        self.assertIn("200.sh", checked["failures"][0])

    def test_custom_recipe_drift_is_explicit(self) -> None:
        for release, text in ((self.source_old, "old recipe\n"), (self.source_new, "new recipe\n")):
            recipe = release / "install/arm64/packages/hypr-rdp/PKGBUILD"
            recipe.parent.mkdir(parents=True)
            recipe.write_text(text)
        failures = update_lib.compare_recipe_hashes(self.source_old, self.source_new)
        self.assertEqual(failures, ["custom package recipe changed without an explicit review: hypr-rdp"])


if __name__ == "__main__":
    unittest.main()
