#!/usr/bin/env python3
"""Focused unprivileged tests for the offline installer image verifier."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "verify-installer-image.py"
SPEC = importlib.util.spec_from_file_location("verify_installer_image", MODULE_PATH)
assert SPEC and SPEC.loader
verify = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify
SPEC.loader.exec_module(verify)


BOOT_UUID = "11111111-2222-3333-4444-555555555555"
ROOT_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class FakeRunner:
    def __init__(self, root_source: Path, boot_source: Path) -> None:
        self.root_source = root_source
        self.boot_source = boot_source
        self.commands: list[list[str]] = []
        self.fail_boot_mount = False
        self.malformed_loop_output = False
        self.loop_back_file = ""
        self.include_lost_loop = False
        self._loop_inventory_calls = 0
        self.partition_document = {
            "partitiontable": {
                "label": "dos",
                "unit": "sectors",
                "partitions": [
                    {"start": 2048, "size": 1024, "type": "c", "bootable": True},
                    {"start": 3072, "size": 4096, "type": "83"},
                ],
            }
        }

    def __call__(self, command: list[str], *, check: bool, **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[:2] == ["sfdisk", "--json"]:
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(self.partition_document), stderr="")
        if command[:3] == ["losetup", "--list", "--json"]:
            self._loop_inventory_calls += 1
            loops: list[dict[str, object]] = []
            if self.include_lost_loop:
                loops.append({"name": "/dev/loop1 (lost)", "back-file": "/old/image.img", "ro": True})
            if self.malformed_loop_output and self._loop_inventory_calls > 1:
                loops.append({"name": "/dev/loop8", "back-file": self.loop_back_file, "ro": True})
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"loopdevices": loops}), stderr="")
        if command[:3] == ["losetup", "--find", "--show"]:
            output = "malformed loop output\n" if self.malformed_loop_output else "/dev/loop7\n"
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")
        if command[:1] == ["blkid"]:
            device = command[-1]
            field = command[2]
            values = {
                ("/dev/loop7p1", "TYPE"): "vfat\n",
                ("/dev/loop7p1", "UUID"): BOOT_UUID + "\n",
                ("/dev/loop7p2", "TYPE"): "ext4\n",
                ("/dev/loop7p2", "UUID"): ROOT_UUID + "\n",
            }
            return subprocess.CompletedProcess(command, 0, stdout=values[(device, field)], stderr="")
        if command[:1] == ["mount"]:
            source = command[-2]
            destination = Path(command[-1])
            if source == "/dev/loop7p1":
                if self.fail_boot_mount:
                    raise subprocess.CalledProcessError(1, command)
                shutil.copytree(self.boot_source, destination, dirs_exist_ok=True, symlinks=True)
            else:
                shutil.copytree(self.root_source, destination, dirs_exist_ok=True, symlinks=True)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:1] == ["umount"] or command[:2] == ["losetup", "--detach"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {command}")


class VerifyInstallerImageTests(unittest.TestCase):
    def make_fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Path, Path, Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        image = root / "installer.img"
        image.write_bytes(b"image")
        root_source = root / "root-source"
        boot_source = root / "boot-source"
        (root_source / "etc/ssh").mkdir(parents=True)
        (root_source / "var/lib/dbus").mkdir(parents=True)
        (root_source / "boot").mkdir()
        (root_source / "etc/fstab").write_text(
            f"UUID={ROOT_UUID} / ext4 defaults 0 1\nUUID={BOOT_UUID} /boot vfat defaults 0 2\n",
            encoding="utf-8",
        )
        marker = root_source / verify.BUILDER_MARKER
        marker.parent.mkdir(parents=True)
        marker.write_bytes(verify.BUILDER_MARKER_CONTENT)
        for unit in verify.SYSTEM_UNITS:
            path = root_source / "etc/systemd/system" / unit
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("[Unit]\n", encoding="utf-8")
        for unit in verify.USER_UNITS:
            path = root_source / "etc/systemd/user" / unit
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("[Unit]\n", encoding="utf-8")
        firstboot_mask = root_source / "etc/systemd/system/systemd-firstboot.service"
        firstboot_mask.parent.mkdir(parents=True, exist_ok=True)
        firstboot_mask.symlink_to("/dev/null")
        network_manager = root_source / "usr/lib/systemd/system/NetworkManager.service"
        sshd = root_source / "usr/lib/systemd/system/sshd.service"
        network_manager.parent.mkdir(parents=True, exist_ok=True)
        network_manager.write_text("[Unit]\n", encoding="utf-8")
        sshd.write_text("[Unit]\n", encoding="utf-8")
        for relative, destination in verify.EXPECTED_LINKS.items():
            link = root_source / relative
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(destination)

        boot_source.mkdir()
        (boot_source / "dtbs/broadcom").mkdir(parents=True)
        (boot_source / "overlays").mkdir()
        (boot_source / "installer-settings.example.toml").write_bytes(
            (HERE / "installer-settings.example.toml").read_bytes()
        )
        (boot_source / "config.txt").write_text(
            "[pi5]\n"
            "dtparam=pciex1_gen=1\n"
            "[all]\n"
            "kernel=kernel8.img\n"
            "initramfs initramfs-linux.img followkernel\n"
            "dtoverlay=vc4-kms-v3d-pi5\n",
            encoding="utf-8",
        )
        (boot_source / "cmdline.txt").write_text(
            f"console=serial0,115200 root=UUID={ROOT_UUID} rootfstype=ext4 rootwait\n",
            encoding="utf-8",
        )
        kernel = bytearray(64)
        kernel[verify.ARM64_IMAGE_MAGIC_OFFSET : verify.ARM64_IMAGE_MAGIC_OFFSET + 4] = verify.ARM64_IMAGE_MAGIC
        (boot_source / "kernel8.img").write_bytes(kernel + b"kernel")
        (boot_source / "initramfs-linux.img").write_bytes(b"initramfs")
        (boot_source / "dtbs/broadcom/bcm2712-rpi-5-b.dtb").write_bytes(b"dtb")
        (boot_source / "overlays/vc4-kms-v3d-pi5.dtbo").write_bytes(b"overlay")
        return temporary, image, root_source, boot_source

    def test_valid_image_is_verified_and_all_mounts_are_read_only_and_cleaned(self) -> None:
        temporary, image, root_source, boot_source = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        runner = FakeRunner(root_source, boot_source)
        runner.include_lost_loop = True

        result = verify.verify_image(image, example=HERE / "installer-settings.example.toml", runner=runner)

        self.assertEqual(result.kernel, "kernel8.img")
        self.assertEqual(result.boot_uuid, BOOT_UUID)
        self.assertEqual(result.root_uuid, ROOT_UUID)
        mount_commands = [command for command in runner.commands if command[:1] == ["mount"]]
        self.assertEqual(len(mount_commands), 2)
        self.assertTrue(all("--read-only" in command for command in mount_commands))
        self.assertEqual(runner.commands[-3][0], "umount")
        self.assertEqual(runner.commands[-2][0], "umount")
        self.assertEqual(runner.commands[-1][:2], ["losetup", "--detach"])

    def test_device_and_symlink_inputs_are_rejected_before_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            image = directory / "image"
            image.write_bytes(b"image")
            link = directory / "link"
            link.symlink_to(image)
            runner = FakeRunner(directory, directory)
            with self.assertRaisesRegex(verify.ImageVerificationError, "symlink"):
                verify.verify_image(link, runner=runner)
            self.assertEqual(runner.commands, [])
            with self.assertRaisesRegex(verify.ImageVerificationError, "under /dev"):
                verify.validate_image_file(Path("/dev/null"))

    def test_private_settings_or_cloned_identity_fails_and_cleans_up(self) -> None:
        temporary, image, root_source, boot_source = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        (boot_source / "installer-settings.toml").write_text("private", encoding="utf-8")
        runner = FakeRunner(root_source, boot_source)

        with self.assertRaisesRegex(verify.ImageVerificationError, "private installer-settings"):
            verify.verify_image(image, runner=runner)
        self.assertEqual(runner.commands[-3][0], "umount")
        self.assertEqual(runner.commands[-2][0], "umount")
        self.assertEqual(runner.commands[-1][:2], ["losetup", "--detach"])

        (boot_source / "installer-settings.toml").unlink()
        (root_source / "etc/machine-id").write_text("cloned\n", encoding="utf-8")
        runner = FakeRunner(root_source, boot_source)
        with self.assertRaisesRegex(verify.ImageVerificationError, "cloned or private identity"):
            verify.verify_image(image, runner=runner)

        (root_source / "etc/machine-id").unlink()
        (root_source / "etc/ssh/ssh_host_ed25519_key").write_text("cloned", encoding="utf-8")
        runner = FakeRunner(root_source, boot_source)
        with self.assertRaisesRegex(verify.ImageVerificationError, "cloned SSH host keys"):
            verify.verify_image(image, runner=runner)

    def test_interactive_firstboot_must_be_masked(self) -> None:
        temporary, image, root_source, boot_source = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        mask = root_source / "etc/systemd/system/systemd-firstboot.service"
        mask.unlink()
        mask.write_text("[Service]\n", encoding="utf-8")
        runner = FakeRunner(root_source, boot_source)

        with self.assertRaisesRegex(verify.ImageVerificationError, "systemd-firstboot.service"):
            verify.verify_image(image, runner=runner)

    def test_legacy_kernel_fails_closed(self) -> None:
        temporary, image, root_source, boot_source = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        (boot_source / "kernel8.img").write_bytes(b"U-Boot legacy payload")
        runner = FakeRunner(root_source, boot_source)

        with self.assertRaisesRegex(verify.ImageVerificationError, "ARM64 Linux Image"):
            verify.verify_image(image, runner=runner)
        self.assertEqual(runner.commands[-3][0], "umount")
        self.assertEqual(runner.commands[-2][0], "umount")
        self.assertEqual(runner.commands[-1][:2], ["losetup", "--detach"])

    def test_boot_mount_failure_unmounts_root_and_detaches_loop(self) -> None:
        temporary, image, root_source, boot_source = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        runner = FakeRunner(root_source, boot_source)
        runner.fail_boot_mount = True

        with self.assertRaises(verify.ImageVerificationError):
            verify.verify_image(image, runner=runner)
        self.assertEqual(runner.commands[-2][0], "umount")
        self.assertEqual(runner.commands[-1][:2], ["losetup", "--detach"])

    def test_malformed_losetup_output_detaches_only_unique_new_matching_loop(self) -> None:
        temporary, image, root_source, boot_source = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        runner = FakeRunner(root_source, boot_source)
        runner.malformed_loop_output = True
        runner.loop_back_file = str(image)

        with self.assertRaisesRegex(verify.ImageVerificationError, "unsafe loop device"):
            verify.verify_image(image, runner=runner)
        self.assertEqual(runner.commands[-1], ["losetup", "--detach", "/dev/loop8"])

    def test_invalid_dos_layout_is_rejected_before_loop_attachment(self) -> None:
        temporary, image, root_source, boot_source = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        runner = FakeRunner(root_source, boot_source)
        runner.partition_document["partitiontable"]["label"] = "gpt"

        with self.assertRaisesRegex(verify.ImageVerificationError, "DOS/MBR"):
            verify.verify_image(image, runner=runner)
        self.assertEqual(len(runner.commands), 1)


if __name__ == "__main__":
    unittest.main()
