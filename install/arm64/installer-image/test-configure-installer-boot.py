#!/usr/bin/env python3
"""Focused tests for isolated staged Pi boot configuration."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("configure-installer-boot.py")
SPEC = importlib.util.spec_from_file_location("configure_installer_boot", MODULE_PATH)
assert SPEC and SPEC.loader
configure = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = configure
SPEC.loader.exec_module(configure)


class FakeRunner:
    def __init__(self, returncode: int = 0, listing: str | None = None) -> None:
        self.returncode = returncode
        self.listing = listing or "usr/lib/modules/x/kernel/drivers/usb/storage/usb-storage.ko\nusr/lib/modules/x/kernel/drivers/usb/storage/uas.ko\nusr/lib/modules/x/kernel/drivers/usb/host/xhci-pci.ko\nusr/lib/modules/x/kernel/fs/ext4/ext4.ko\n"
        self.commands: list[list[str]] = []

    def __call__(
        self,
        command: list[str],
        *,
        check: bool,
        capture_output: bool = False,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        stdout = self.listing if command[-2:] == ["-l", "/boot/initramfs-linux.img"] else ""
        return subprocess.CompletedProcess(command, self.returncode, stdout=stdout)


class ConfigureInstallerBootTests(unittest.TestCase):
    def make_root(
        self,
        *,
        kernel8: bool = True,
        kernel_2712: bool = False,
        dtb_name: str = "bcm2712-rpi-5-b.dtb",
    ) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        boot = root / "boot"
        (boot / "dtbs/broadcom").mkdir(parents=True)
        (boot / "overlays").mkdir()
        (root / "etc").mkdir()
        (boot / "dtbs/broadcom" / dtb_name).write_bytes(b"dtb")
        (boot / "overlays/vc4-kms-v3d-pi5.dtbo").write_bytes(b"overlay")
        (boot / "initramfs-linux.img").write_bytes(b"initramfs")
        linux_image = bytearray(64)
        linux_image[0x38:0x3C] = b"ARM\x64"
        if kernel8:
            (boot / "kernel8.img").write_bytes(linux_image + b"kernel8")
        if kernel_2712:
            (boot / "kernel_2712.img").write_bytes(linux_image + b"kernel2712")
        (root / "etc/mkinitcpio.conf").write_text("# target mkinitcpio configuration\n", encoding="utf-8")
        (root / "etc/mkinitcpio.d").mkdir()
        (root / "etc/mkinitcpio.d/linux-rpi.preset").write_text(
            "#ALL_config=\"/etc/mkinitcpio.conf\"\nPRESETS=('default')\n",
            encoding="utf-8",
        )
        (root / "lib/modules/6.18.53-1-rpi").mkdir(parents=True)
        (root / "lib/modules/6.18.53-1-rpi/modules.builtin").write_text("", encoding="utf-8")
        return root

    def test_stages_pi5_config_and_usb_safe_mkinitcpio_without_touching_cmdline(self) -> None:
        root = self.make_root()
        config = root / "boot/config.txt"
        config.write_text(
            "camera_auto_detect=1\n"
            "kernel=old-kernel.img\n"
            "initramfs old.img\n"
            "dtoverlay=vc4-kms-v3d-pi5\n"
            "dtparam=pciex1_gen=2\n",
            encoding="utf-8",
        )
        cmdline = root / "boot/cmdline.txt"
        cmdline.write_text("console=serial0,115200 root=UUID=staged rw rootwait\n", encoding="utf-8")

        result = configure.configure_installer_boot(root)

        self.assertEqual(result.kernel, "kernel8.img")
        self.assertEqual(result.dtb, "dtbs/broadcom/bcm2712-rpi-5-b.dtb")
        self.assertFalse(result.generated)
        rendered = config.read_text(encoding="utf-8")
        self.assertIn("camera_auto_detect=1", rendered)
        self.assertEqual(rendered.count("kernel=kernel8.img"), 1)
        self.assertEqual(rendered.count("initramfs initramfs-linux.img followkernel"), 1)
        self.assertEqual(rendered.count("dtoverlay=vc4-kms-v3d-pi5"), 1)
        self.assertIn("[pi5]\ndtparam=pciex1_gen=1\n[all]", rendered)
        self.assertNotIn("kernel=old-kernel.img", rendered)
        self.assertNotIn("dtparam=pciex1_gen=2", rendered)
        self.assertEqual(cmdline.read_text(encoding="utf-8"), "console=serial0,115200 root=UUID=staged rw rootwait\n")
        fragment = root / configure.MKINITCPIO_FRAGMENT
        fragment_text = fragment.read_text(encoding="utf-8")
        self.assertIn("xhci_pci", fragment_text)
        self.assertIn("usb_storage", fragment_text)
        self.assertIn("uas", fragment_text)
        self.assertIn("ext4", fragment_text)
        self.assertIn("HOOKS=(base systemd modconf keyboard sd-vconsole block filesystems fsck)", fragment_text)
        self.assertNotIn("autodetect", fragment_text.lower())
        self.assertNotIn("kms", fragment_text.lower())

    def test_selects_pi5_kernel_when_kernel8_is_unavailable(self) -> None:
        root = self.make_root(kernel8=False, kernel_2712=True)
        result = configure.configure_installer_boot(root)
        self.assertEqual(result.kernel, "kernel_2712.img")
        self.assertIn("kernel=kernel_2712.img", (root / "boot/config.txt").read_text(encoding="utf-8"))

    def test_accepts_cm5_dtb_without_forcing_firmware_tree_selection(self) -> None:
        root = self.make_root(dtb_name="bcm2712-rpi-cm5-cm5io.dtb")

        result = configure.configure_installer_boot(root)

        self.assertEqual(result.dtb, "dtbs/broadcom/bcm2712-rpi-cm5-cm5io.dtb")
        rendered = (root / "boot/config.txt").read_text(encoding="utf-8")
        self.assertNotIn("device_tree=", rendered)

    def test_explicit_generation_uses_native_nspawn_and_no_argv_secrets(self) -> None:
        root = self.make_root()
        tool = root / "usr/bin/mkinitcpio"
        tool.parent.mkdir(parents=True)
        tool.write_bytes(b"target mkinitcpio")
        tool.chmod(0o755)
        lsinitcpio = root / "usr/bin/lsinitcpio"
        lsinitcpio.write_bytes(b"target lsinitcpio")
        lsinitcpio.chmod(0o755)
        runner = FakeRunner()

        result = configure.configure_installer_boot(root, generate=True, runner=runner, machine="aarch64")

        self.assertTrue(result.generated)
        self.assertEqual(
            runner.commands,
            [
                [
                    "systemd-nspawn",
                    "-D",
                    str(root),
                    "--register=no",
                    "--private-network",
                    "/usr/bin/mkinitcpio",
                    "-P",
                ],
                [
                    "systemd-nspawn",
                    "-D",
                    str(root),
                    "--register=no",
                    "--private-network",
                    "/usr/bin/lsinitcpio",
                    "-l",
                    "/boot/initramfs-linux.img",
                ],
            ],
        )

    def test_generation_refuses_non_native_host(self) -> None:
        root = self.make_root()
        tool = root / "usr/bin/mkinitcpio"
        tool.parent.mkdir(parents=True)
        tool.write_bytes(b"target mkinitcpio")
        (root / "usr/bin/lsinitcpio").write_bytes(b"target lsinitcpio")
        with self.assertRaises(configure.BootConfigurationError):
            configure.configure_installer_boot(root, generate=True, runner=FakeRunner(), machine="x86_64")

    def test_generation_rejects_initramfs_without_required_usb_modules(self) -> None:
        root = self.make_root()
        tool = root / "usr/bin/mkinitcpio"
        tool.parent.mkdir(parents=True)
        tool.write_bytes(b"target mkinitcpio")
        (root / "usr/bin/lsinitcpio").write_bytes(b"target lsinitcpio")
        with self.assertRaises(configure.BootConfigurationError):
            configure.configure_installer_boot(
                root,
                generate=True,
                runner=FakeRunner(listing="usr/lib/modules/x/kernel/fs/ext4.ko\n"),
                machine="aarch64",
            )

    def test_generation_accepts_usb_and_ext4_drivers_built_into_target_kernel(self) -> None:
        root = self.make_root()
        tool = root / "usr/bin/mkinitcpio"
        tool.parent.mkdir(parents=True)
        tool.write_bytes(b"target mkinitcpio")
        (root / "usr/bin/lsinitcpio").write_bytes(b"target lsinitcpio")
        (root / "lib/modules/6.18.53-1-rpi/modules.builtin").write_text(
            "kernel/drivers/usb/storage/usb-storage.ko\n"
            "kernel/drivers/usb/storage/uas.ko\n"
            "kernel/drivers/usb/host/xhci-pci.ko\n"
            "kernel/fs/ext4/ext4.ko\n",
            encoding="utf-8",
        )
        result = configure.configure_installer_boot(
            root,
            generate=True,
            runner=FakeRunner(listing="usr/lib/modules/x/kernel/fs/fat.ko\n"),
            machine="aarch64",
        )
        self.assertTrue(result.generated)

    def test_merged_usr_lib_link_is_followed_only_when_exact(self) -> None:
        root = self.make_root()
        version = "6.18.53-1-rpi"
        modules = root / "lib/modules" / version
        (modules / "modules.builtin").unlink()
        modules.rmdir()
        (root / "lib/modules").rmdir()
        (root / "lib").rmdir()
        (root / "usr/lib/modules" / version).mkdir(parents=True)
        (root / "usr/lib/modules" / version / "modules.builtin").write_text("", encoding="utf-8")
        (root / "lib").symlink_to("usr/lib")

        versions = configure._kernel_module_versions(root)

        self.assertEqual(versions, [root / "usr/lib/modules" / version])
        (root / "lib").unlink()
        (root / "lib").symlink_to("etc")
        with self.assertRaisesRegex(configure.BootConfigurationError, "merged-/usr"):
            configure._kernel_module_versions(root)

    def test_staged_luks_cmdline_is_rejected_before_any_boot_write(self) -> None:
        root = self.make_root()
        cmdline = root / "boot/cmdline.txt"
        cmdline.write_text("root=UUID=staged rd.luks.uuid=private rw\n", encoding="utf-8")
        with self.assertRaises(configure.BootConfigurationError):
            configure.configure_installer_boot(root)
        self.assertFalse((root / "boot/config.txt").exists())
        self.assertFalse((root / configure.MKINITCPIO_FRAGMENT).exists())

    def test_fat_symlink_is_rejected_before_any_boot_write(self) -> None:
        root = self.make_root()
        os.symlink("kernel8.img", root / "boot/kernel-link.img")
        with self.assertRaises(configure.BootConfigurationError):
            configure.configure_installer_boot(root)
        self.assertFalse((root / "boot/config.txt").exists())

    def test_stale_uboot_kernel_is_not_replaced_by_an_image_fallback(self) -> None:
        root = self.make_root()
        (root / "boot/kernel8.img").write_bytes(b"U-Boot legacy image")
        image = bytearray(64)
        image[0x38:0x3c] = b"ARM\x64"
        (root / "boot/Image").write_bytes(image)
        with self.assertRaises(configure.BootConfigurationError):
            configure.configure_installer_boot(root)
        self.assertFalse((root / "boot/config.txt").exists())

    def test_nondefault_linux_rpi_preset_config_is_rejected(self) -> None:
        root = self.make_root()
        (root / "etc/mkinitcpio.d/linux-rpi.preset").write_text(
            'ALL_config="/etc/other-mkinitcpio.conf"\n', encoding="utf-8"
        )
        with self.assertRaises(configure.BootConfigurationError):
            configure.configure_installer_boot(root)
        self.assertFalse((root / "boot/config.txt").exists())

    def test_existing_pacman_metadata_must_own_selected_kernel(self) -> None:
        root = self.make_root()
        record = root / "var/lib/pacman/local/linux-rpi-6.18.53-1"
        record.mkdir(parents=True)
        (record / "files").write_text("%FILES%\nboot/kernel8.img\n", encoding="utf-8")
        configure.configure_installer_boot(root)
        (record / "files").write_text("%FILES%\nboot/other.img\n", encoding="utf-8")
        (root / "boot/config.txt").unlink()
        with self.assertRaises(configure.BootConfigurationError):
            configure.configure_installer_boot(root)
        self.assertFalse((root / "boot/config.txt").exists())

    def test_missing_dtb_or_overlay_fails_closed(self) -> None:
        root = self.make_root()
        (root / "boot/overlays/vc4-kms-v3d-pi5.dtbo").unlink()
        with self.assertRaises(configure.BootConfigurationError):
            configure.configure_installer_boot(root)
        self.assertFalse((root / "boot/config.txt").exists())

    def test_host_root_is_never_accepted(self) -> None:
        with self.assertRaises(configure.BootConfigurationError):
            configure.configure_installer_boot(Path("/"))


if __name__ == "__main__":
    unittest.main()
