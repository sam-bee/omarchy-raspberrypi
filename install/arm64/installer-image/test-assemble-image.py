#!/usr/bin/env python3
"""Focused, unprivileged tests for assemble-image.py."""

from __future__ import annotations

import base64
import io
import importlib.util
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("assemble-image.py")
SPEC = importlib.util.spec_from_file_location("assemble_image", MODULE_PATH)
assert SPEC and SPEC.loader
assemble_image = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = assemble_image
SPEC.loader.exec_module(assemble_image)


class AssembleImageTests(unittest.TestCase):
    def test_layout_has_mbr_gap_boot_and_root(self) -> None:
        layout = assemble_image.make_layout(512, 1024)
        self.assertEqual(layout.boot_start_sector, 2048)
        self.assertEqual(layout.boot_size_mib, 512)
        self.assertEqual(layout.root_start_sector, 2048 + (512 * 1024 * 1024 // 512))
        self.assertEqual(layout.image_bytes, (layout.root_start_sector + layout.root_sectors) * 512)
        script = assemble_image.partition_script(layout)
        self.assertIn("label: dos", script)
        self.assertIn("type=c, bootable", script)
        self.assertIn("type=83", script)

    def test_fstab_replaces_root_and_boot_entries(self) -> None:
        original = "# keep this\nLABEL=old / ext4 rw 0 1\n/dev/mmcblk0p1 /boot vfat defaults 0 2\nUUID=data /data ext4 defaults 0 2\n"
        rewritten = assemble_image.rewrite_fstab(original, root_uuid="root-new", boot_uuid="boot-new")
        self.assertIn("# keep this", rewritten)
        self.assertIn("UUID=data /data", rewritten)
        self.assertIn("UUID=root-new / ext4 defaults 0 1", rewritten)
        self.assertIn("UUID=boot-new /boot vfat defaults 0 2", rewritten)
        self.assertNotIn("LABEL=old /", rewritten)
        self.assertNotIn("/dev/mmcblk0p1 /boot", rewritten)

    def test_cmdline_replaces_old_root_and_adds_required_arguments(self) -> None:
        rewritten = assemble_image.rewrite_cmdline(
            "console=serial0,115200 root=PARTUUID=old rootfstype=btrfs root=UUID=duplicate quiet",
            root_uuid="new-root",
        )
        self.assertEqual(rewritten.count("root=UUID=new-root"), 1)
        self.assertIn("rootfstype=ext4", rewritten)
        self.assertIn("rootwait", rewritten)
        self.assertNotIn("PARTUUID=old", rewritten)
        self.assertNotIn("rootfstype=btrfs", rewritten)

    def test_config_forces_gen1_and_creates_config_when_empty(self) -> None:
        self.assertEqual(
            assemble_image.ensure_gen1_config("[all]\ndtparam=pciex1_gen=2 # old\n"),
            "[all]\ndtparam=pciex1_gen=1 # old\n",
        )
        generated = assemble_image.ensure_gen1_config("")
        self.assertIn("[pi5]", generated)
        self.assertIn("dtparam=pciex1_gen=1", generated)

    def test_empty_staging_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(assemble_image.ImageAssemblyError):
                assemble_image.validate_staging_directory(Path(temporary))

    def test_staging_needs_nonempty_kernel_and_initramfs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary)
            boot = staging / "boot"
            boot.mkdir()
            (staging / "etc").mkdir()
            (boot / "kernel8.img").write_bytes(b"")
            (boot / "initramfs-linux.img").write_bytes(b"init")
            with self.assertRaisesRegex(assemble_image.ImageAssemblyError, "kernel"):
                assemble_image.validate_staging_directory(staging)
            (boot / "kernel8.img").write_bytes(b"kernel")
            self.assertEqual(assemble_image.validate_staging_directory(staging), staging.resolve())

    def test_recommended_root_size_uses_conservative_default_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary)
            (staging / "usr").mkdir()
            (staging / "usr/payload").write_bytes(b"payload")
            self.assertEqual(assemble_image.DEFAULT_ROOT_EXTRA_MIB, 1024)
            self.assertEqual(assemble_image.recommended_root_size_mib(staging), 1282)
            self.assertGreaterEqual(assemble_image.estimate_ext4_payload_bytes(staging), 3 * 4096)

    def test_fat_boot_tree_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary)
            (staging / "etc").mkdir()
            boot = staging / "boot"
            boot.mkdir()
            (boot / "kernel8.img").write_bytes(b"kernel")
            (boot / "initramfs-linux.img").write_bytes(b"init")
            (boot / "firmware-link").symlink_to("kernel8.img")
            with self.assertRaisesRegex(assemble_image.ImageAssemblyError, "symlink unsupported by FAT"):
                assemble_image.validate_staging_directory(staging)

    def test_root_copy_preserves_hardlinks_modes_owners_xattrs_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            staging = directory / "staging"
            root_mount = directory / "root"
            staging.mkdir()
            root_mount.mkdir()
            (staging / "etc").mkdir()
            (staging / "usr/bin").mkdir(parents=True)
            (staging / "boot").mkdir()
            (staging / "boot/should-not-enter-root").write_bytes(b"boot")
            source = staging / "etc/preserved"
            source.write_bytes(b"metadata")
            os.chmod(source, 0o640)
            if os.geteuid() == 0:
                os.chown(source, 65534, 65534)
            os.link(source, staging / "etc/preserved-hardlink")
            (staging / "bin").symlink_to("usr/bin")
            xattr_name = "user.omarchy_test"
            xattr_value = b"retained"
            xattr_supported = True
            try:
                os.setxattr(source, xattr_name, xattr_value)
            except OSError:
                xattr_supported = False

            assemble_image.copy_root_without_boot(staging, root_mount)
            copied = root_mount / "etc/preserved"
            copied_hardlink = root_mount / "etc/preserved-hardlink"
            self.assertEqual(stat.S_IMODE(copied.stat().st_mode), 0o640)
            self.assertEqual(copied.stat().st_uid, source.stat().st_uid)
            self.assertEqual(copied.stat().st_gid, source.stat().st_gid)
            self.assertEqual(copied.stat().st_ino, copied_hardlink.stat().st_ino)
            self.assertTrue((root_mount / "bin").is_symlink())
            self.assertEqual(os.readlink(root_mount / "bin"), "usr/bin")
            self.assertFalse((root_mount / "boot").exists())
            if xattr_supported:
                self.assertEqual(os.getxattr(copied, xattr_name), xattr_value)

    def test_tar_copy_does_not_deadlock_when_producer_stderr_exceeds_pipe_buffer(self) -> None:
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as stream:
            data = b"producer payload\n"
            info = tarfile.TarInfo("etc/from-producer")
            info.size = len(data)
            info.uid = os.getuid()
            info.gid = os.getgid()
            stream.addfile(info, io.BytesIO(data))
        encoded = base64.b64encode(archive.getvalue()).decode("ascii")
        module_path = str(MODULE_PATH.resolve())
        helper = f'''\
import base64
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile

spec = importlib.util.spec_from_file_location("assemble_image_child", {module_path!r})
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)
real_popen = subprocess.Popen

def producer(command, *args, **kwargs):
    if command[:2] == ["tar", "--create"]:
        program = "import os,base64; os.write(2,b'x'*200000); os.write(1,base64.b64decode(" + repr({encoded!r}) + "))"
        return real_popen([sys.executable, "-c", program], *args, **kwargs)
    return real_popen(command, *args, **kwargs)

module.subprocess.Popen = producer
with tempfile.TemporaryDirectory() as temporary:
    staging = Path(temporary) / "staging"
    root = Path(temporary) / "root"
    (staging / "boot").mkdir(parents=True)
    (staging / "etc").mkdir()
    root.mkdir()
    (staging / "etc/source").write_bytes(b"source")
    module._run_tar_copy(staging, root)
    assert (root / "etc/from-producer").read_bytes() == b"producer payload\\n"
'''
        with tempfile.TemporaryDirectory() as temporary:
            helper_path = Path(temporary) / "tar-stderr-overflow.py"
            helper_path.write_text(helper, encoding="utf-8")
            child = subprocess.Popen(
                [sys.executable, str(helper_path)],
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            try:
                stdout, stderr = child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.communicate()
                self.fail("tar copy hung while producer stderr exceeded a pipe buffer")
        self.assertEqual(child.returncode, 0, stderr or stdout)

    def test_tar_copy_terminates_producer_when_extraction_is_interrupted(self) -> None:
        class Producer:
            def __init__(self) -> None:
                self.stdout = io.BytesIO()
                self.terminated = False
                self.waited = False

            def poll(self) -> int | None:
                return -15 if self.terminated else None

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.terminated = True

            def wait(self, timeout: float | None = None) -> int:
                self.waited = True
                return -15

        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary) / "staging"
            root = Path(temporary) / "root"
            (staging / "etc").mkdir(parents=True)
            (staging / "etc/source").write_bytes(b"source")
            root.mkdir()
            producer = Producer()
            with mock.patch.object(assemble_image.subprocess, "Popen", return_value=producer), mock.patch.object(
                assemble_image.subprocess, "run", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    assemble_image._run_tar_copy(staging, root)
            self.assertTrue(producer.terminated)
            self.assertTrue(producer.waited)

    def test_boot_only_staging_is_rejected_as_an_empty_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary)
            boot = staging / "boot"
            boot.mkdir()
            (boot / "kernel8.img").write_bytes(b"kernel")
            (boot / "initramfs-linux.img").write_bytes(b"init")
            with self.assertRaisesRegex(assemble_image.ImageAssemblyError, "root files"):
                assemble_image.validate_staging_directory(staging)

    def test_special_files_in_staging_are_rejected_before_assembly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary)
            (staging / "boot").mkdir()
            (staging / "boot/kernel8.img").write_bytes(b"kernel")
            (staging / "boot/initramfs-linux.img").write_bytes(b"init")
            (staging / "etc").mkdir()
            fifo = staging / "etc/unsafe-device"
            fifo.parent.mkdir(parents=True, exist_ok=True)
            os.mkfifo(fifo)
            with self.assertRaisesRegex(assemble_image.ImageAssemblyError, "special file"):
                assemble_image.validate_staging_directory(staging)

    def test_existing_and_special_tree_outputs_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            existing = directory / "existing.img"
            existing.write_bytes(b"already here")
            with self.assertRaises(assemble_image.ImageAssemblyError):
                assemble_image.validate_output_path(existing)
            with self.assertRaises(assemble_image.ImageAssemblyError):
                assemble_image.validate_output_path(Path("/dev/omarchy-image-that-must-not-exist"))

            redirected = directory / "redirected"
            redirected.mkdir()
            (directory / "output-link").symlink_to(redirected, target_is_directory=True)
            with self.assertRaisesRegex(assemble_image.ImageAssemblyError, "symlink path component"):
                assemble_image.validate_output_path(directory / "output-link" / "image.img")
            with self.assertRaisesRegex(assemble_image.ImageAssemblyError, "symlink path component"):
                assemble_image.validate_output_path(directory / "output-link" / ".." / "image.img")

    def test_mkfs_fat_executable_requires_native_elf_and_help(self) -> None:
        executable = Path(sys.executable).resolve()
        with mock.patch.object(
            assemble_image.subprocess,
            "run",
            wraps=assemble_image.subprocess.run,
        ) as run:
            self.assertEqual(
                assemble_image.validate_mkfs_fat_executable(executable),
                os.fspath(executable),
            )
        self.assertEqual(run.call_args.args[0][-1], "--help")
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "mkfs.fat"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)
            with self.assertRaisesRegex(assemble_image.ImageAssemblyError, "64-bit ELF"):
                assemble_image.validate_mkfs_fat_executable(script)

    def test_output_inside_staging_is_rejected_before_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            staging = directory / "staging"
            (staging / "boot").mkdir(parents=True)
            (staging / "etc").mkdir()
            (staging / "boot/kernel8.img").write_bytes(b"kernel")
            (staging / "boot/initramfs-linux.img").write_bytes(b"init")
            output = staging / "generated.img"
            with self.assertRaisesRegex(assemble_image.ImageAssemblyError, "inside the staging"):
                assemble_image.assemble_image(staging, output)
            self.assertFalse(output.exists())

    def test_created_image_is_a_new_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "image.img"
            assemble_image._create_output(output, 4096)
            self.assertTrue(output.is_file())
            self.assertEqual(output.stat().st_size, 4096)
            with self.assertRaises(FileExistsError):
                assemble_image._create_output(output, 4096)

    def test_identity_files_are_removed_but_user_keys_remain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc/ssh").mkdir(parents=True)
            (root / "var/lib/dbus").mkdir(parents=True)
            (root / "etc/machine-id").write_text("host-id\n", encoding="utf-8")
            (root / "var/lib/dbus/machine-id").write_text("host-id\n", encoding="utf-8")
            (root / "etc/ssh/ssh_host_rsa_key").write_text("secret\n", encoding="utf-8")
            (root / "etc/ssh/ssh_host_rsa_key.pub").write_text("public\n", encoding="utf-8")
            (root / "etc/ssh/authorized_keys").write_text("user-key\n", encoding="utf-8")
            assemble_image.sanitize_identity_files(root)
            self.assertFalse((root / "etc/machine-id").exists())
            self.assertFalse((root / "var/lib/dbus/machine-id").exists())
            self.assertFalse((root / "etc/ssh/ssh_host_rsa_key").exists())
            self.assertTrue((root / "etc/ssh/authorized_keys").exists())

    def test_interactive_firstboot_is_masked_for_headless_boot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc").mkdir()

            assemble_image.mask_interactive_firstboot(root)

            mask = root / "etc/systemd/system/systemd-firstboot.service"
            self.assertTrue(mask.is_symlink())
            self.assertEqual(mask.readlink(), Path("/dev/null"))

    def test_interactive_firstboot_mask_replaces_a_stale_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mask = root / "etc/systemd/system/systemd-firstboot.service"
            mask.parent.mkdir(parents=True)
            mask.write_text("[Service]\n", encoding="utf-8")

            assemble_image.mask_interactive_firstboot(root)

            self.assertTrue(mask.is_symlink())
            self.assertEqual(mask.readlink(), Path("/dev/null"))


if __name__ == "__main__":
    unittest.main()
