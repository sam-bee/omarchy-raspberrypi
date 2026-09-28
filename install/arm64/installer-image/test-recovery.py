#!/usr/bin/env python3
"""File-backed contract tests for the bounded USB recovery helper."""

from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("recovery", HERE / "recovery.py")
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = recovery
SPEC.loader.exec_module(recovery)


LUKS_UUID = "12345678-1234-1234-1234-123456789abc"
BOOT_UUID = "ABCD-1234"


class FixtureRunner:
    def __init__(self, source_root: Path) -> None:
        self.source_root = source_root
        self.bad_initramfs_listing = False
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self.inventory = {
            "blockdevices": [
                {
                    "path": "/dev/sda",
                    "kname": "sda",
                    "type": "disk",
                    "size": 64 * 1024 * 1024 * 1024,
                    "serial": "fixture-target-1",
                    "tran": "usb",
                    "children": [
                        {"path": "/dev/sda1", "kname": "sda1", "type": "part", "fstype": "vfat", "label": "PI-BOOT", "uuid": BOOT_UUID},
                        {"path": "/dev/sda2", "kname": "sda2", "type": "part", "fstype": "crypto_LUKS", "label": "OMARCHY-ROOT", "uuid": LUKS_UUID},
                    ],
                },
                {
                    "path": "/dev/sdb",
                    "kname": "sdb",
                    "type": "disk",
                    "size": 16 * 1024 * 1024 * 1024,
                    "serial": "fixture-installer-1",
                    "tran": "usb",
                    "label": "OMARCHY-INSTALLER",
                    "children": [
                        {"path": "/dev/sdb1", "kname": "sdb1", "type": "part", "fstype": "vfat", "label": "OMARCHY-INSTALLER", "uuid": "AAAA-BBBB"},
                    ],
                },
            ]
        }

    def __call__(self, command: list[str], *, input: str | None, text: bool, capture_output: bool, check: bool) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        self.inputs.append(input)
        if command[:2] == ["lsblk", "--json"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(self.inventory), "")
        if command[:1] == ["findmnt"]:
            return subprocess.CompletedProcess(command, 0, json.dumps({"filesystems": []}), "")
        if command[:2] == ["cat", "/proc/swaps"]:
            return subprocess.CompletedProcess(command, 0, "Filename\tType\tSize\tUsed\tPriority\n", "")
        if command[:1] == ["udevadm"]:
            return subprocess.CompletedProcess(command, 1, "", "")
        if command[:2] == ["cryptsetup", "luksUUID"]:
            return subprocess.CompletedProcess(command, 0, LUKS_UUID + "\n", "")
        if command[:2] == ["cryptsetup", "open"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["cryptsetup", "close"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:1] == ["mount"]:
            mountpoint = Path(command[-1])
            if command[-2] == "/dev/mapper/omarchy-pi-recovery-cryptroot":
                shutil.copytree(self.source_root, mountpoint, dirs_exist_ok=True)
            elif command[-2] == "/dev/sda1":
                destination = mountpoint
                destination.mkdir(parents=True, exist_ok=True)
                for item in (self.source_root / "boot").iterdir():
                    target = destination / item.name
                    if item.is_file():
                        shutil.copy2(item, target)
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:1] in (["install"], ["/usr/bin/install"]):
            source = Path(command[-2])
            destination = Path(command[-1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:1] == ["umount"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:1] == ["systemd-nspawn"]:
            root = Path(command[command.index("--directory") + 1])
            if "/usr/bin/pacman-key" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            if "/usr/bin/pacman" in command and "-Qp" in command:
                return subprocess.CompletedProcess(command, 0, "linux-rpi 6.1-1 aarch64\n", "")
            if "/usr/bin/install" in command:
                source = root / command[-2].lstrip("/")
                destination = root / command[-1].lstrip("/")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            if command[-2:] == ["/usr/bin/mkinitcpio", "-P"]:
                (root / "boot/initramfs-linux.img").write_bytes(b"repaired-initramfs")
            if command[-4:-1] == ["/usr/bin/pacman", "--noconfirm", "-U"]:
                (root / "boot/kernel8.img").write_bytes(b"restored-kernel")
            if command[-3:] == ["/usr/bin/lsinitcpio", "-l", "/boot/initramfs-linux.img"]:
                listing = "usr/lib/modules/6.0-rpi/kernel/ext4.ko\n" if self.bad_initramfs_listing else "usr/lib/modules/6.1-rpi/kernel/ext4.ko\n"
                return subprocess.CompletedProcess(command, 0, listing, "")
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected command: {command}")


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        (self.source / "boot").mkdir(parents=True)
        (self.source / "etc").mkdir()
        (self.source / "etc/mkinitcpio.d").mkdir(parents=True)
        (self.source / "usr/lib/omarchy-pi").mkdir(parents=True)
        (self.source / "boot/cmdline.txt").write_text("root=UUID=" + LUKS_UUID + " rw rootwait\n", encoding="utf-8")
        (self.source / "boot/config.txt").write_text("dtparam=pciex1_gen=2\nkernel=kernel8.img\n", encoding="utf-8")
        (self.source / "etc/crypttab").write_text("cryptroot UUID=" + LUKS_UUID + " none\n", encoding="utf-8")
        (self.source / "etc/mkinitcpio.d/linux-rpi.preset").write_text("ALL_kver='/usr/lib/modules/6.1-rpi'\nPRESETS=('default')\n", encoding="utf-8")
        (self.source / "usr/lib/omarchy-pi/installer-provenance.json").write_text(json.dumps({"source_revision": "a" * 40}), encoding="utf-8")
        package_dir = self.source / "var/lib/pacman/local/linux-rpi-6.1-1"
        package_dir.mkdir(parents=True)
        (package_dir / "desc").write_text("%NAME%\nlinux-rpi\n\n%VERSION%\n6.1-1\n\n%ARCH%\naarch64\n", encoding="utf-8")
        archive = self.source / "var/cache/pacman/pkg/linux-rpi-6.1-1-aarch64.pkg.tar.zst"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"signed-fixture-archive")
        (archive.parent / (archive.name + ".sig")).write_bytes(b"signed-fixture-signature")
        modules = self.source / "usr/lib/modules/6.1-rpi"
        modules.mkdir(parents=True)
        (modules / "pkgbase").write_text("linux-rpi\n", encoding="utf-8")
        image = modules / "vmlinuz"
        image.write_bytes(b"installed-package-kernel")
        (package_dir / "files").write_text("%FILES%\nboot/kernel8.img\nusr/lib/modules/6.1-rpi/\nusr/lib/modules/6.1-rpi/pkgbase\nusr/lib/modules/6.1-rpi/vmlinuz\n\n", encoding="utf-8")
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        (package_dir / "mtree").write_text(
            f"#mtree\n./usr/lib/modules/6.1-rpi/vmlinuz type=file size={image.stat().st_size} sha256digest={digest}\n",
            encoding="utf-8",
        )
        self.runner = FixtureRunner(self.source)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def target(self) -> recovery.TargetIdentity:
        return recovery.discover_targets(runner=self.runner)[0]

    def test_discover_refuses_installer_media_and_returns_stable_token(self) -> None:
        response = recovery.handle_request({"action": "recovery-discover"}, runner=self.runner)
        self.assertEqual(response["targets"][0]["stable_id"], "serial:fixture-target-1")
        self.assertIn("RECOVER /dev/sda serial:fixture-target-1", response["targets"][0]["token"])
        installer = response["targets"][1]
        self.assertFalse(installer["eligible"])
        self.assertIn("installer media", installer["reasons"])

    def test_wrong_target_and_missing_confirmation_are_rejected(self) -> None:
        target = self.target()
        with self.assertRaisesRegex(recovery.RecoveryError, "confirmation"):
            recovery.handle_request({"action": "recovery-inspect", "target": target.path}, runner=self.runner)
        installer_token = next(item for item in recovery.discover_targets(runner=self.runner) if item.path == "/dev/sdb").token
        with self.assertRaisesRegex(recovery.RecoveryError, "refused"):
            recovery.handle_request(
                {"action": "recovery-inspect", "target": "/dev/sdb", "target_confirmation": installer_token},
                runner=self.runner,
            )

    def test_mounted_target_is_refused_before_unlock(self) -> None:
        self.runner.inventory["blockdevices"][0]["children"][1]["mountpoints"] = ["/"]
        target = self.target()
        self.assertIn("mounted filesystem or descendant", target.reasons)
        with self.assertRaisesRegex(recovery.RecoveryError, "refused"):
            recovery.handle_request(
                {"action": "recovery-plan", "target": target.path, "target_confirmation": target.token, "passphrase": "fixture passphrase"},
                runner=self.runner,
            )
        self.assertFalse(any(call[:2] == ["cryptsetup", "open"] for call in self.runner.calls))

    def test_plan_is_read_only_and_keeps_passphrase_out_of_argv(self) -> None:
        target = self.target()
        result = recovery.handle_request(
            {"action": "recovery-plan", "target": target.path, "target_confirmation": target.token, "passphrase": "fixture passphrase"},
            runner=self.runner,
        )
        self.assertTrue(result["read_only"])
        self.assertTrue(result["unlock_plan"]["read_only"])
        self.assertNotIn("fixture passphrase", result["unlock_plan"]["command"])
        self.assertNotIn("mount", [call[0] for call in self.runner.calls])

    def test_inspect_opens_read_only_mounts_and_cleans_everything(self) -> None:
        target = self.target()
        response = recovery.handle_request(
            {"action": "recovery-inspect", "target": target.path, "target_confirmation": target.token, "passphrase": "fixture passphrase"},
            runner=self.runner,
            mount_root=self.base / "mounts",
        )
        self.assertEqual(response["inspection"]["source_revision"], "a" * 40)
        self.assertTrue(response["inspection"]["crypttab_present"])
        open_call = next(call for call in self.runner.calls if call[:2] == ["cryptsetup", "open"])
        self.assertIn("--readonly", open_call)
        root_mount = next(call for call in self.runner.calls if call[:1] == ["mount"] and "noload" in call[2])
        self.assertIn("ro,noload", root_mount)
        self.assertTrue(any(call[:1] == ["cryptsetup"] and call[1] == "close" for call in self.runner.calls))
        self.assertGreaterEqual(sum(call[:1] == ["umount"] for call in self.runner.calls), 2)
        self.assertIn("fixture passphrase", [call for call in self.runner.inputs if call])
        self.assertTrue(all("fixture passphrase" not in " ".join(call) for call in self.runner.calls))

    def test_key_file_unlock_uses_existing_file_and_no_keyslot_operation(self) -> None:
        key = self.base / "existing.key"
        key.write_bytes(b"existing-key")
        key.chmod(0o600)
        target = self.target()
        mapper = recovery.unlock_target(target, key_file=key, runner=self.runner)
        self.assertIsNotNone(mapper)
        open_call = next(call for call in self.runner.calls if call[:2] == ["cryptsetup", "open"])
        self.assertIn(str(key), open_call)
        self.assertNotIn("luksAddKey", " ".join(call for call in self.runner.calls for call in call))
        mapper.close()  # type: ignore[union-attr]

    def test_repair_requires_confirmation_reopens_writable_and_preserves_config(self) -> None:
        target = self.target()
        request = {"action": "recovery-repair", "target": target.path, "target_confirmation": target.token, "passphrase": "fixture passphrase"}
        with self.assertRaisesRegex(recovery.RecoveryError, "explicit confirmation"):
            recovery.handle_request(request, runner=self.runner, mount_root=self.base / "mounts")
        before_cmdline = (self.source / "boot/cmdline.txt").read_bytes()
        before_config = (self.source / "boot/config.txt").read_bytes()
        response = recovery.handle_request({**request, "repair_confirmation": recovery.REPAIR_CONFIRMATION}, runner=self.runner, mount_root=self.base / "mounts")
        self.assertTrue(response["preserved_unchanged"])
        self.assertTrue(response["missing_kernel"])
        open_call = next(call for call in self.runner.calls if call[:2] == ["cryptsetup", "open"] and "--readonly" not in call)
        self.assertNotIn("--readonly", open_call)
        self.assertTrue(any(call[:1] == ["systemd-nspawn"] and "/usr/bin/install" in call and "/usr/lib/modules/6.1-rpi/vmlinuz" in call for call in self.runner.calls))
        self.assertEqual((self.source / "boot/cmdline.txt").read_bytes(), before_cmdline)
        self.assertEqual((self.source / "boot/config.txt").read_bytes(), before_config)

    def test_repair_plan_does_not_rewrite_boot_config(self) -> None:
        plan = recovery.plan_boot_repair(self.source, self.source / "boot")
        self.assertTrue(plan.missing_kernel)
        self.assertEqual(plan.commands[0][0], "systemd-nspawn")
        self.assertIn("/usr/bin/install", plan.commands[0])
        self.assertFalse(plan.verification_commands)
        self.assertEqual(plan.restore_source, "installed-module-vmlinuz")
        self.assertIn("--timezone=off", plan.commands[0])
        self.assertIn("--bind=" + str(self.source / "boot") + ":/boot", plan.commands[0])
        self.assertTrue(any(command[0] == "systemd-nspawn" and "/usr/bin/mkinitcpio" in command for command in plan.commands))
        self.assertNotIn("config.txt", " ".join(" ".join(command) for command in plan.commands))

    def test_merged_usr_lib_symlink_is_one_module_tree(self) -> None:
        (self.source / "lib").symlink_to("usr/lib", target_is_directory=True)
        plan = recovery.plan_boot_repair(self.source, self.source / "boot")
        self.assertEqual(plan.module_version, "6.1-rpi")

    def test_mismatched_preset_module_tree_is_refused_before_repair(self) -> None:
        preset = self.source / "etc/mkinitcpio.d/linux-rpi.preset"
        preset.write_text("ALL_kver='/usr/lib/modules/6.2-rpi'\nPRESETS=('default')\n", encoding="utf-8")
        module_dir = self.source / "usr/lib/modules/6.2-rpi"
        module_dir.mkdir()
        (module_dir / "pkgbase").write_text("linux-rpi\n", encoding="utf-8")
        with self.assertRaisesRegex(recovery.RecoveryError, "not package-owned"):
            recovery.plan_boot_repair(self.source, self.source / "boot")

    def test_repair_rejects_initramfs_for_wrong_module_version(self) -> None:
        self.runner.bad_initramfs_listing = True
        with self.assertRaisesRegex(recovery.RecoveryError, "selected linux-rpi modules"):
            recovery.repair_target(self.source, self.source / "boot", runner=self.runner)

    def test_missing_kernel_uses_exact_installed_vmlinuz_without_cache(self) -> None:
        shutil.rmtree(self.source / "var/cache/pacman/pkg")
        plan = recovery.plan_boot_repair(self.source, self.source / "boot")
        self.assertEqual(plan.restore_source, "installed-module-vmlinuz")
        self.assertFalse(plan.verification_commands)

    def test_missing_vmlinuz_uses_exact_signed_archive_fallback(self) -> None:
        (self.source / "usr/lib/modules/6.1-rpi/vmlinuz").unlink()
        plan = recovery.plan_boot_repair(self.source, self.source / "boot")
        self.assertEqual(plan.restore_source, "signed-local-archive")
        self.assertTrue(plan.verification_commands)

    def test_empty_vmlinuz_uses_verified_installer_boot_kernel(self) -> None:
        image = self.source / "usr/lib/modules/6.1-rpi/vmlinuz"
        image.write_bytes(b"")
        installer_boot = self.base / "installer-boot"
        installer_boot.mkdir()
        source = installer_boot / "kernel8.img"
        source.write_bytes(b"installer-package-kernel")
        package_dir = self.source / "var/lib/pacman/local/linux-rpi-6.1-1"
        (package_dir / "mtree").write_text(
            "#mtree\n"
            "./usr/lib/modules/6.1-rpi/vmlinuz type=file size=0 sha256digest="
            + hashlib.sha256(b"").hexdigest()
            + "\n"
            + "./boot/kernel8.img type=file size="
            + str(source.stat().st_size)
            + " sha256digest="
            + hashlib.sha256(source.read_bytes()).hexdigest()
            + "\n",
            encoding="utf-8",
        )
        plan = recovery.plan_boot_repair(self.source, self.source / "boot", installer_kernel=source)
        self.assertEqual(plan.restore_source, "installer-boot-kernel8")
        self.assertEqual(plan.restore_source_path, str(source))
        self.assertEqual(plan.restore_size, source.stat().st_size)
        self.assertEqual(plan.restore_digest, hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(plan.commands[0][:4], ("/usr/bin/install", "--mode=0644", "--preserve-timestamps", "--"))
        self.assertNotIn("systemd-nspawn", plan.commands[0])

    def test_repair_copies_verified_installer_boot_kernel_and_checks_digest(self) -> None:
        image = self.source / "usr/lib/modules/6.1-rpi/vmlinuz"
        image.write_bytes(b"")
        installer_boot = self.base / "installer-boot"
        installer_boot.mkdir()
        source = installer_boot / "kernel8.img"
        source.write_bytes(b"installer-package-kernel")
        package_dir = self.source / "var/lib/pacman/local/linux-rpi-6.1-1"
        (package_dir / "mtree").write_text(
            "#mtree\n"
            "./usr/lib/modules/6.1-rpi/vmlinuz type=file size=0 sha256digest="
            + hashlib.sha256(b"").hexdigest()
            + "\n"
            + "./boot/kernel8.img type=file size="
            + str(source.stat().st_size)
            + " sha256digest="
            + hashlib.sha256(source.read_bytes()).hexdigest()
            + "\n",
            encoding="utf-8",
        )
        report = recovery.repair_target(self.source, self.source / "boot", installer_kernel=source, runner=self.runner)
        self.assertEqual(report["restore_source"], "installer-boot-kernel8")
        self.assertEqual((self.source / "boot/kernel8.img").read_bytes(), source.read_bytes())
        self.assertTrue(any(command[:1] in (["install"], ["/usr/bin/install"]) for command in self.runner.calls))

    def test_tampered_installed_vmlinuz_is_refused(self) -> None:
        (self.source / "usr/lib/modules/6.1-rpi/vmlinuz").write_bytes(b"tampered")
        with self.assertRaisesRegex(recovery.RecoveryError, "digest verification"):
            recovery.plan_boot_repair(self.source, self.source / "boot")

    def test_cleanup_retains_failed_resources_for_a_safe_retry(self) -> None:
        root_mount = self.base / "root-mount"
        boot_mount = self.base / "boot-mount"
        root_mount.mkdir()
        boot_mount.mkdir()
        attempts = {str(boot_mount): 0, str(root_mount): 0}

        def cleanup_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "umount":
                path = command[-1]
                attempts[path] += 1
                if path == str(boot_mount) and attempts[path] == 1:
                    return subprocess.CompletedProcess(command, 1, "", "busy")
            return subprocess.CompletedProcess(command, 0, "", "")

        lease = recovery.MountLease(root_mount, boot_mount, cleanup_runner)
        with self.assertRaisesRegex(recovery.RecoveryError, "cleanup failed"):
            lease.cleanup()
        self.assertFalse(lease.cleaned)
        lease.cleanup()
        self.assertTrue(lease.cleaned)

    def test_standalone_cli_starts_in_isolated_python(self) -> None:
        cli = HERE / "omarchy-pi-recover"
        result = subprocess.run([str(cli), "--help"], env={"PATH": "/usr/bin:/bin"}, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("recovery", result.stdout)


if __name__ == "__main__":
    unittest.main()
