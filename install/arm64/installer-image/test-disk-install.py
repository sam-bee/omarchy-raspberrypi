#!/usr/bin/env python3
"""Unprivileged command-boundary tests for disk_install.py."""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


HERE = Path(__file__).parent
SPEC = spec_from_file_location("disk_install", HERE / "disk_install.py")
assert SPEC and SPEC.loader
disk_install = module_from_spec(SPEC)
SPEC.loader.exec_module(disk_install)


GIB = 1024 * 1024 * 1024


def _result(command: list[str], stdout: str = "", *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")


def _graph_fixture() -> tuple[dict, dict, dict, dict]:
    size = 64 * GIB
    key_uuid = "7695adc1-3681-459a-894f-80f1b615d430"
    document = {
        "blockdevices": [
            {
                "path": "/dev/nvme0n1", "kname": "nvme0n1", "type": "disk", "size": size,
                "serial": "NVME-TARGET", "tran": "nvme", "log-sec": 512, "children": [
                    {"path": "/dev/nvme0n1p1", "kname": "nvme0n1p1", "type": "part", "size": 1 * GIB,
                     "fstype": "vfat", "label": "PI-BOOT", "mountpoints": ["/boot"]},
                    {"path": "/dev/nvme0n1p2", "kname": "nvme0n1p2", "type": "part", "size": 63 * GIB,
                     "fstype": "ext4", "mountpoints": ["/"]},
                ],
            },
            {
                "path": "/dev/sda", "kname": "sda", "type": "disk", "size": size,
                "serial": "DEDICATED-KEY", "tran": "usb", "log-sec": 512, "children": [
                    {"path": "/dev/sda1", "kname": "sda1", "type": "part", "size": 256 * 1024 * 1024,
                     "fstype": "ext4", "label": "OMARCHYKEY", "uuid": key_uuid},
                ],
            },
            {
                "path": "/dev/sdb", "kname": "sdb", "type": "disk", "size": size,
                "serial": "FRESH-KEY", "tran": "usb", "log-sec": 512, "children": [],
            },
            {
                "path": "/dev/sdc", "kname": "sdc", "type": "disk", "size": size,
                "serial": "MOUNTED-DATA", "tran": "usb", "log-sec": 512, "children": [
                    {"path": "/dev/sdc1", "kname": "sdc1", "type": "part", "size": size,
                     "fstype": "ext4", "mountpoints": ["/mnt/data"]},
                ],
            },
        ]
    }
    mounts = {
        "filesystems": [
            {"target": "/", "source": "/dev/nvme0n1p2", "fstype": "ext4"},
            {"target": "/boot", "source": "/dev/nvme0n1p1", "fstype": "vfat"},
            {"target": "/mnt/data", "source": "/dev/sdc1", "fstype": "ext4"},
        ]
    }
    return document, mounts, {
        "/dev/nvme0n1": {"path": "/dev/nvme0n1", "identity": {"stable_id": "serial:NVME-TARGET", "size": size}, "size": size, "eligible": True, "key_eligible": False, "reasons": []},
    }, {
        "path": "/dev/sdb", "identity": {"stable_id": "serial:FRESH-KEY", "size": size, "blank_key_candidate": True}, "size": size, "eligible": True, "key_eligible": True, "reasons": [],
    }


class DiskInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document, self.mounts, self.target, self.blank_key = _graph_fixture()

    def fixture_runner(self, command: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        if command[0] == "lsblk":
            return _result(command, json.dumps(self.document))
        if command[0] == "findmnt":
            return _result(command, json.dumps(self.mounts))
        if command[0] == "udevadm":
            return _result(command)
        return _result(command)

    def discovery_patches(self):
        return mock.patch.multiple(
            disk_install,
            _run=mock.Mock(side_effect=self.fixture_runner),
            _preflight_error=mock.Mock(return_value=None),
            _swap_sources=mock.Mock(return_value=set()),
        )

    def test_discovery_protects_mounted_root_key_and_other_mounts(self) -> None:
        with self.discovery_patches():
            disks = {disk["path"]: disk for disk in disk_install.discover_disks()}

        self.assertFalse(disks["/dev/nvme0n1"]["eligible"])
        self.assertTrue(any("mounted" in reason for reason in disks["/dev/nvme0n1"]["reasons"]))
        self.assertFalse(disks["/dev/sda"]["eligible"])
        self.assertTrue(any("unlock-key" in reason for reason in disks["/dev/sda"]["reasons"]))
        self.assertFalse(disks["/dev/sdc"]["eligible"])
        self.assertTrue(disks["/dev/sdb"]["eligible"])
        self.assertTrue(disks["/dev/sdb"]["key_eligible"])

    def test_discovery_applies_target_boot_fallback_to_disk_eligibility(self) -> None:
        document = {
            "blockdevices": [
                {
                    "path": "/dev/nvme0n1", "kname": "nvme0n1", "type": "disk", "size": 64 * GIB,
                    "serial": "NVME-TARGET", "tran": "nvme", "log-sec": 512, "children": [],
                },
                {
                    "path": "/dev/mmcblk0", "kname": "mmcblk0", "type": "disk", "size": 64 * GIB,
                    "serial": "MMC-TARGET", "tran": "mmc", "log-sec": 512, "children": [],
                },
                {
                    "path": "/dev/sdb", "kname": "sdb", "type": "disk", "size": 64 * GIB,
                    "serial": "FRESH-KEY", "tran": "usb", "log-sec": 512, "children": [],
                },
            ],
        }

        def runner(command: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
            if command[0] == "lsblk":
                return _result(command, json.dumps(document))
            if command[0] == "findmnt":
                return _result(command, json.dumps({"filesystems": []}))
            if command[0] == "udevadm":
                return _result(command)
            raise AssertionError(f"unexpected discovery command: {command}")

        def preflight(target: dict | None = None) -> str | None:
            if target and target.get("kname") == "mmcblk0":
                return "EEPROM boot order has no MMC/SD fallback for this target"
            return None

        with mock.patch.multiple(
            disk_install,
            _run=mock.Mock(side_effect=runner),
            _preflight_error=mock.Mock(side_effect=preflight),
            _swap_sources=mock.Mock(return_value=set()),
            _sysfs_details=mock.Mock(return_value={"available": True, "holders": [], "slaves": [], "partition": False, "ro": False}),
        ):
            disks = {disk["path"]: disk for disk in disk_install.discover_disks()}

        self.assertTrue(disks["/dev/nvme0n1"]["eligible"])
        self.assertFalse(disks["/dev/mmcblk0"]["eligible"])
        self.assertIn("MMC/SD fallback", disks["/dev/mmcblk0"]["reasons"][-1])
        self.assertTrue(disks["/dev/sdb"]["key_eligible"])

    def test_selection_token_contains_path_and_stable_identity(self) -> None:
        with self.discovery_patches():
            selected = disk_install.select_disk("/dev/sdb")
            token = disk_install.confirm_token(selected)
        self.assertEqual(token, "ERASE /dev/sdb serial:FRESH-KEY")

    def test_key_selection_allows_a_blank_512_mib_usb_but_not_target_role(self) -> None:
        small_key = json.loads(json.dumps(self.blank_key))
        small_key["size"] = 512 * 1024 * 1024
        small_key["identity"]["size"] = small_key["size"]
        small_key["identity"]["blank_key_candidate"] = True
        small_key["eligible"] = False
        small_key["reasons"] = ["device is too small"]
        with mock.patch.object(disk_install, "_preflight_error", return_value=None), mock.patch.object(
            disk_install, "discover_disks", return_value=[small_key]
        ):
            self.assertEqual(disk_install.select_key_disk("/dev/sdb")["path"], "/dev/sdb")
            with self.assertRaisesRegex(disk_install.InstallError, "device is too small"):
                disk_install.select_disk("/dev/sdb")

    def test_existing_key_media_cannot_be_reselected_as_fresh_key(self) -> None:
        with self.discovery_patches():
            disks = {disk["path"]: disk for disk in disk_install.discover_disks()}
            with self.assertRaisesRegex(disk_install.InstallError, "blank USB"):
                disk_install.validate_pair(disks["/dev/sdb"], disks["/dev/sda"])

    def test_sysfs_holder_is_a_target_guard(self) -> None:
        def details(kname: str) -> dict[str, object]:
            return {"available": True, "holders": ["dm-9"] if kname == "sdb" else [], "slaves": [], "partition": False, "ro": False}

        with self.discovery_patches(), mock.patch.object(disk_install, "_sysfs_details", side_effect=details):
            disks = {disk["path"]: disk for disk in disk_install.discover_disks()}
        self.assertFalse(disks["/dev/sdb"]["eligible"])
        self.assertTrue(any("holders" in reason for reason in disks["/dev/sdb"]["reasons"]))

    def test_full_path_kname_and_pkname_use_sysfs_components(self) -> None:
        calls: list[str] = []

        def details(kname: str) -> dict[str, object]:
            calls.append(kname)
            return {"available": True, "holders": [], "slaves": [], "partition": False, "ro": False}

        document = {
            "blockdevices": [{
                "path": "/dev/nvme0n1",
                "kname": "/dev/nvme0n1",
                "type": "disk",
                "size": 64 * GIB,
                "serial": "FULL-PATH-TARGET",
                "tran": "nvme",
                "log-sec": 512,
                "children": [{
                    "path": "/dev/nvme0n1p1",
                    "kname": "/dev/nvme0n1p1",
                    "pkname": "/dev/nvme0n1",
                    "type": "part",
                    "size": 1 * GIB,
                }],
            }],
        }
        with mock.patch.object(disk_install, "_sysfs_details", side_effect=details):
            nodes = disk_install._flatten(document["blockdevices"])
        self.assertEqual(nodes[0]["kname"], "nvme0n1")
        self.assertEqual(nodes[0]["children"][0]["kname"], "nvme0n1p1")
        self.assertEqual(nodes[0]["children"][0]["pkname"], "nvme0n1")
        self.assertEqual(set(calls), {"nvme0n1", "nvme0n1p1"})

    def test_findmnt_alias_is_resolved_by_major_minor(self) -> None:
        target = {
            "path": "/dev/nvme0n1",
            "type": "disk",
            "maj:min": "259:0",
            "children": [{
                "path": "/dev/nvme0n1p2",
                "type": "part",
                "pkname": "nvme0n1",
                "maj:min": "259:2",
                "children": [],
            }],
        }
        nodes = [target, target["children"][0]]
        mounts = [{
            "target": "/",
            "source": "/dev/disk/by-uuid/rootfs",
            "maj:min": "259:2",
        }]
        reasons = disk_install._reason_mounts(target, disk_install._node_path_index(nodes), mounts)
        self.assertEqual(reasons, ["mounted source /dev/disk/by-uuid/rootfs"])

    def test_findmnt_nested_children_are_flattened(self) -> None:
        document = {
            "filesystems": [{
                "target": "/",
                "source": "/dev/nvme0n1p2",
                "children": [{
                    "target": "/boot",
                    "source": "/dev/nvme0n1p1",
                    "children": [{"target": "/boot/firmware", "source": "/dev/nvme0n1p1"}],
                }],
            }],
        }
        with mock.patch.object(disk_install, "_checked", return_value=_result(["findmnt"], json.dumps(document))):
            mounts = disk_install._findmnt_mounts()
        self.assertEqual([mount["target"] for mount in mounts], ["/", "/boot", "/boot/firmware"])
        self.assertTrue(all("children" not in mount for mount in mounts))

    def test_graph_requests_explicit_lsblk_tree(self) -> None:
        calls: list[list[str]] = []

        def checked(command: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if command[0] == "lsblk":
                return _result(command, json.dumps({"blockdevices": []}))
            if command[0] == "findmnt":
                return _result(command, json.dumps({"filesystems": []}))
            raise AssertionError(f"unexpected graph command: {command}")

        with mock.patch.object(disk_install, "_checked", side_effect=checked), mock.patch.object(
            disk_install, "_swap_sources", return_value=set()
        ), mock.patch.object(disk_install, "_preflight_error", return_value=None):
            disk_install._graph()
        lsblk = next(command for command in calls if command[0] == "lsblk")
        self.assertIn("--tree", lsblk)

    def test_stable_identity_swap_is_rejected(self) -> None:
        replacement = dict(self.target["/dev/nvme0n1"])
        replacement["identity"] = dict(replacement["identity"], stable_id="serial:REPLACEMENT")
        with mock.patch.object(disk_install, "_preflight_error", return_value=None), mock.patch.object(
            disk_install, "discover_disks", return_value=[replacement]
        ):
            with self.assertRaisesRegex(disk_install.InstallError, "disappeared or changed"):
                disk_install.validate_pair(self.target["/dev/nvme0n1"])

    def test_preflight_accepts_usb_first_nvme_fallback_without_changing_eeprom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "marker"
            model = Path(temporary) / "model"
            marker.write_bytes(disk_install.INSTALLER_MARKER_CONTENT)
            model.write_text("Raspberry Pi 5 Model B", encoding="utf-8")

            def runner(command: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
                if command[:2] == ["uname", "-m"]:
                    return _result(command, "aarch64\n")
                if command[:2] == ["vcgencmd", "bootloader_config"]:
                    return _result(command, "BOOT_ORDER: 0xf64\n")
                raise AssertionError(f"unexpected preflight command: {command}")

            with mock.patch.multiple(
                disk_install,
                _run=mock.Mock(side_effect=runner),
                INSTALLER_MARKER=marker,
                MODEL_PATH=model,
            ), mock.patch.object(disk_install.os, "geteuid", return_value=0):
                self.assertIsNone(disk_install._preflight_error())

    def test_preflight_matches_cm5_fallback_to_selected_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "marker"
            model = Path(temporary) / "model"
            marker.write_bytes(disk_install.INSTALLER_MARKER_CONTENT)
            model.write_text("Raspberry Pi Compute Module 5 Rev 1.0", encoding="utf-8")

            cases = (
                ("0xf64", {"kname": "nvme0n1"}, None),
                ("0xf64", {"kname": "mmcblk0"}, "MMC/SD"),
                ("0xf14", {"kname": "mmcblk0"}, None),
                ("0xf14", {"kname": "nvme0n1"}, "NVMe"),
                ("0xf15", {"kname": "mmcblk0"}, None),
                ("0xf15", {"kname": "nvme0n1"}, "NVMe"),
            )
            for order, target, expected_reason in cases:
                with self.subTest(order=order, target=target["kname"]):
                    commands: list[list[str]] = []

                    def runner(command: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
                        commands.append(command)
                        if command[:2] == ["uname", "-m"]:
                            return _result(command, "aarch64\n")
                        if command[:2] == ["vcgencmd", "bootloader_config"]:
                            return _result(command, f"BOOT_ORDER: {order}\n")
                        raise AssertionError(f"unexpected preflight command: {command}")

                    with mock.patch.multiple(
                        disk_install,
                        _run=mock.Mock(side_effect=runner),
                        INSTALLER_MARKER=marker,
                        MODEL_PATH=model,
                    ), mock.patch.object(disk_install.os, "geteuid", return_value=0):
                        error = disk_install._preflight_error(target)
                    if expected_reason is None:
                        self.assertIsNone(error)
                    else:
                        self.assertIn(expected_reason, error or "")
                    # Preflight reads the EEPROM configuration only; it must
                    # never invoke a write or update command.
                    self.assertEqual(
                        commands,
                        [["uname", "-m"], ["vcgencmd", "bootloader_config"]],
                    )

    def test_preflight_preserves_pi5_nvme_fallback_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "marker"
            model = Path(temporary) / "model"
            marker.write_bytes(disk_install.INSTALLER_MARKER_CONTENT)
            model.write_text("Raspberry Pi 5 Model B Rev 1.0", encoding="utf-8")

            def runner(command: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
                if command[:2] == ["uname", "-m"]:
                    return _result(command, "aarch64\n")
                if command[:2] == ["vcgencmd", "bootloader_config"]:
                    return _result(command, "BOOT_ORDER: 0xf14\n")
                raise AssertionError(f"unexpected preflight command: {command}")

            with mock.patch.multiple(
                disk_install,
                _run=mock.Mock(side_effect=runner),
                INSTALLER_MARKER=marker,
                MODEL_PATH=model,
            ), mock.patch.object(disk_install.os, "geteuid", return_value=0):
                self.assertIn("USB-first boot order", disk_install._preflight_error({"kname": "mmcblk0"}) or "")

    def test_preflight_rejects_unknown_board_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "marker"
            model = Path(temporary) / "model"
            marker.write_bytes(disk_install.INSTALLER_MARKER_CONTENT)

            def runner(command: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
                if command[:2] == ["uname", "-m"]:
                    return _result(command, "aarch64\n")
                raise AssertionError(f"unexpected preflight command: {command}")

            for value in ("Raspberry Pi 4 Model B", "Raspberry Pi Compute Module 50 Rev 1.0"):
                with self.subTest(model=value):
                    model.write_text(value, encoding="utf-8")
                    with mock.patch.multiple(
                        disk_install,
                        _run=mock.Mock(side_effect=runner),
                        INSTALLER_MARKER=marker,
                        MODEL_PATH=model,
                    ), mock.patch.object(disk_install.os, "geteuid", return_value=0):
                        self.assertIn("Raspberry Pi 5 or Compute Module 5", disk_install._preflight_error() or "")

    def test_debugfs_key_probe_uses_real_stat_output_without_mounting(self) -> None:
        item = {"path": "/dev/sdd1", "fstype": "ext4", "mountpoints": []}
        result = _result(["debugfs"], "Inode: 42  Type: regular  Mode: 0400\n")
        with mock.patch.object(disk_install, "_run", return_value=result) as runner:
            self.assertTrue(disk_install._debugfs_has_path(item, "/.cryptroot.key"))
        runner.assert_called_once_with(["debugfs", "-R", "stat /.cryptroot.key", "--", "/dev/sdd1"], check=False)

    def test_plain_preparation_uses_aligned_layout_and_owned_cleanup(self) -> None:
        commands: list[tuple[list[str], str | None]] = []

        def runner(command: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
            commands.append((command, input_text))
            if command[0] == "blkid":
                return _result(command, "11111111-1111-1111-1111-111111111111\n" if command[-1].endswith("p2") else "22222222-2222-2222-2222-222222222222\n")
            return _result(command)

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.multiple(
                disk_install,
                _run=mock.Mock(side_effect=runner),
                _preflight_error=mock.Mock(return_value=None),
                discover_disks=mock.Mock(return_value=[self.target["/dev/nvme0n1"]]),
                MOUNT_BASE=Path(temporary) / "mnt",
                SECRET_BASE=Path(temporary) / "run",
            ):
                with disk_install.prepare_target(self.target["/dev/nvme0n1"], "plain", None) as context:
                    self.assertTrue(str(context["root"]).startswith(temporary))
                    self.assertTrue(context["root"].is_dir())
                    self.assertTrue(context["boot"].is_dir())

        sfdisk = next((argv, value) for argv, value in commands if argv[0] == "sfdisk")
        self.assertIn("label: dos", sfdisk[1] or "")
        self.assertIn("first-lba: 2048", sfdisk[1] or "")
        self.assertIn("2097152", sfdisk[1] or "")
        self.assertIn(["mkfs.fat", "-F", "32", "-n", "PI-BOOT", "--", "/dev/nvme0n1p1"], [argv for argv, _ in commands])
        self.assertIn(["mkfs.ext4", "-F", "-U", "random", "-L", "OMARCHY-ROOT", "--", "/dev/nvme0n1p2"], [argv for argv, _ in commands])
        wipefs_index = next(index for index, (argv, _input) in enumerate(commands) if argv[0] == "wipefs")
        self.assertEqual(commands[wipefs_index + 1][0], ["udevadm", "settle", "--timeout=30"])
        self.assertTrue(any(
            argv[:7] == ["mount", "-t", "vfat", "-o", "fmask=0133,dmask=0022", "--", "/dev/nvme0n1p1"]
            and argv[7].endswith("/root/boot")
            for argv, _ in commands
        ))
        self.assertTrue(any(argv[:2] == ["umount", "--"] for argv, _ in commands))

    def test_preparation_progress_reports_static_milestones_in_order(self) -> None:
        commands: list[tuple[list[str], str | None]] = []
        progress: list[str] = []

        def runner(command: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
            commands.append((command, input_text))
            if command[0] == "blkid":
                return _result(command, "11111111-1111-1111-1111-111111111111\n" if command[-1].endswith("p2") else "22222222-2222-2222-2222-222222222222\n")
            return _result(command)

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.multiple(
                disk_install,
                _run=mock.Mock(side_effect=runner),
                _preflight_error=mock.Mock(return_value=None),
                discover_disks=mock.Mock(return_value=[self.target["/dev/nvme0n1"]]),
                MOUNT_BASE=Path(temporary) / "mnt",
                SECRET_BASE=Path(temporary) / "run",
            ):
                with disk_install.prepare_target(
                    self.target["/dev/nvme0n1"],
                    "plain",
                    None,
                    progress_callback=progress.append,
                ):
                    pass

        self.assertEqual(
            progress,
            [
                "Revalidating selected target and key devices",
                "Selected target and key devices revalidated",
                "Rechecking devices immediately before mutation",
                "Devices rechecked immediately before mutation",
                "Preparing target: wiping previous signatures",
                "Target signatures wiped",
                "Preparing target: settling device changes",
                "Target device changes settled",
                "Writing target partition table",
                "Target partition table written",
                "Re-reading target partitions with partprobe",
                "Target partitions re-read with partprobe",
                "Waiting for target partition udev events",
                "Target partition udev events settled",
                "Formatting target boot filesystem (FAT)",
                "Target boot filesystem formatted",
                "Formatting target root filesystem (ext4)",
                "Target root filesystem formatted",
                "Mounting target root filesystem",
                "Target root filesystem mounted",
                "Mounting target boot filesystem",
                "Target boot filesystem mounted",
                "Discovering target root filesystem UUID",
                "Target root filesystem UUID discovered",
                "Discovering target boot filesystem UUID",
                "Target boot filesystem UUID discovered",
                "Target storage preparation complete",
            ],
        )
        self.assertTrue(all("/dev/" not in label and "11111111" not in label for label in progress))

    def test_preparation_progress_stops_before_failed_operation(self) -> None:
        commands: list[list[str]] = []
        progress: list[str] = []

        def runner(command: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if command[0] == "sfdisk":
                raise disk_install.InstallError("command failed: sfdisk")
            return _result(command)

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.multiple(
                disk_install,
                _run=mock.Mock(side_effect=runner),
                _preflight_error=mock.Mock(return_value=None),
                discover_disks=mock.Mock(return_value=[self.target["/dev/nvme0n1"]]),
                MOUNT_BASE=Path(temporary) / "mnt",
                SECRET_BASE=Path(temporary) / "run",
            ):
                with self.assertRaisesRegex(disk_install.InstallError, "sfdisk"):
                    with disk_install.prepare_target(
                        self.target["/dev/nvme0n1"],
                        "plain",
                        None,
                        progress_callback=progress.append,
                    ):
                        self.fail("preparation unexpectedly reached the mount boundary")

        self.assertEqual(progress[-1], "Writing target partition table")
        self.assertNotIn("Target partition table written", progress)
        self.assertNotIn("Formatting target boot filesystem (FAT)", progress)
        self.assertFalse(any(command[0] == "mkfs.fat" for command in commands))

    def test_encrypted_progress_labels_never_include_recovery_secret(self) -> None:
        secret = "recovery-passphrase"
        progress: list[str] = []

        def runner(command: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
            if command[0] == "cryptsetup" and command[1] == "luksFormat":
                raise disk_install.InstallError("command failed: cryptsetup")
            return _result(command)

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.multiple(
                disk_install,
                _run=mock.Mock(side_effect=runner),
                _preflight_error=mock.Mock(return_value=None),
                discover_disks=mock.Mock(return_value=[self.target["/dev/nvme0n1"]]),
                MOUNT_BASE=Path(temporary) / "mnt",
                SECRET_BASE=Path(temporary) / "run",
            ):
                with self.assertRaisesRegex(disk_install.InstallError, "cryptsetup"):
                    with disk_install.prepare_target(
                        self.target["/dev/nvme0n1"],
                        "passphrase",
                        secret,
                        progress_callback=progress.append,
                    ):
                        self.fail("preparation unexpectedly reached the mount boundary")

        self.assertIn("Formatting target root encryption (LUKS)", progress)
        self.assertNotIn("Target root encryption formatted", progress)
        self.assertTrue(all(secret not in label for label in progress))

    def test_progress_failure_after_root_mount_still_cleans_up_mount(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if command[0] == "blkid":
                return _result(command, "11111111-1111-1111-1111-111111111111\n" if command[-1].endswith("p2") else "22222222-2222-2222-2222-222222222222\n")
            return _result(command)

        def progress(label: str) -> None:
            if label == "Target root filesystem mounted":
                raise RuntimeError("progress sink failed")

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.multiple(
                disk_install,
                _run=mock.Mock(side_effect=runner),
                _preflight_error=mock.Mock(return_value=None),
                discover_disks=mock.Mock(return_value=[self.target["/dev/nvme0n1"]]),
                MOUNT_BASE=Path(temporary) / "mnt",
                SECRET_BASE=Path(temporary) / "run",
            ):
                with self.assertRaisesRegex(RuntimeError, "progress sink"):
                    with disk_install.prepare_target(
                        self.target["/dev/nvme0n1"],
                        "plain",
                        None,
                        progress_callback=progress,
                    ):
                        self.fail("progress callback unexpectedly returned")

        self.assertTrue(any(command[:2] == ["umount", "--"] for command in commands))

    def test_post_wipe_settle_failure_stops_target_and_key_mutation(self) -> None:
        """A lost udev view must fail closed before any new partitioning."""

        for mode, key_identity, passphrase in (("plain", None, None), ("key", self.blank_key, "recovery-passphrase")):
            with self.subTest(mode=mode):
                commands: list[list[str]] = []

                def runner(command: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
                    commands.append(command)
                    if command[:2] == ["udevadm", "settle"] and "--timeout=30" in command:
                        raise disk_install.InstallError("command failed: udevadm")
                    return _result(command)

                with tempfile.TemporaryDirectory() as temporary:
                    temporary_root = Path(temporary)
                    with mock.patch.multiple(
                        disk_install,
                        _run=mock.Mock(side_effect=runner),
                        _preflight_error=mock.Mock(return_value=None),
                        discover_disks=mock.Mock(return_value=[self.target["/dev/nvme0n1"], self.blank_key]),
                        MOUNT_BASE=temporary_root / "mnt",
                        SECRET_BASE=temporary_root / "run",
                    ):
                        with self.assertRaisesRegex(disk_install.InstallError, "udevadm"):
                            with disk_install.prepare_target(self.target["/dev/nvme0n1"], mode, passphrase, key_identity):
                                self.fail("preparation unexpectedly reached the mount boundary")
                    self.assertFalse(any(argv[0] in {"sfdisk", "mkfs.fat", "mkfs.ext4", "cryptsetup"} for argv in commands))
                    self.assertFalse(list((temporary_root / "mnt").glob("omarchy-pi-install-*")))
                    self.assertFalse(list((temporary_root / "run").glob("omarchy-pi-install-*")))

    def test_post_wipe_stale_identity_still_fails_closed_before_partitioning(self) -> None:
        """Settling must not weaken the identity guard if the graph stays stale."""

        stale_target = dict(self.target["/dev/nvme0n1"])
        stale_target["identity"] = dict(stale_target["identity"])
        stale_target["eligible"] = False
        stale_target["reasons"] = ["sysfs block graph is unavailable"]
        wiped = False

        def discover() -> list[dict[str, object]]:
            return [stale_target if wiped else self.target["/dev/nvme0n1"]]

        commands: list[list[str]] = []

        def runner(command: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
            nonlocal wiped
            commands.append(command)
            if command[0] == "wipefs":
                wiped = True
            return _result(command)

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            with mock.patch.multiple(
                disk_install,
                _run=mock.Mock(side_effect=runner),
                _preflight_error=mock.Mock(return_value=None),
                discover_disks=mock.Mock(side_effect=discover),
                MOUNT_BASE=temporary_root / "mnt",
                SECRET_BASE=temporary_root / "run",
            ):
                with self.assertRaisesRegex(disk_install.InstallError, "sysfs block graph is unavailable"):
                    with disk_install.prepare_target(self.target["/dev/nvme0n1"], "plain", None):
                        self.fail("preparation unexpectedly reached the mount boundary")
            self.assertIn(["udevadm", "settle", "--timeout=30"], commands)
            self.assertFalse(any(argv[0] in {"sfdisk", "mkfs.fat", "mkfs.ext4"} for argv in commands))
            self.assertFalse(list((temporary_root / "mnt").glob("omarchy-pi-install-*")))
            self.assertFalse(list((temporary_root / "run").glob("omarchy-pi-install-*")))

    def test_progress_callback_cannot_make_destructive_identity_guard_stale(self):
        for milestone, forbidden_command in (
            ("Preparing target: wiping previous signatures", "wipefs"),
            ("Writing target partition table", "sfdisk"),
            ("Formatting target boot filesystem (FAT)", "mkfs.fat"),
            ("Formatting target root filesystem (ext4)", "mkfs.ext4"),
        ):
            with self.subTest(milestone=milestone), tempfile.TemporaryDirectory() as temporary:
                commands = []
                changed = False
                stale = dict(self.target["/dev/nvme0n1"])
                stale["eligible"] = False
                stale["reasons"] = ["device changed while recording progress"]

                def progress(message):
                    nonlocal changed
                    if message == milestone:
                        changed = True

                def discover():
                    return [stale if changed else self.target["/dev/nvme0n1"]]

                def runner(command, *, input_text=None, check=True):
                    commands.append(command)
                    return _result(command)

                with mock.patch.multiple(
                    disk_install,
                    _run=mock.Mock(side_effect=runner),
                    _preflight_error=mock.Mock(return_value=None),
                    discover_disks=mock.Mock(side_effect=discover),
                    MOUNT_BASE=Path(temporary) / "mnt",
                    SECRET_BASE=Path(temporary) / "run",
                ):
                    with self.assertRaisesRegex(disk_install.InstallError, "device changed while recording progress"):
                        with disk_install.prepare_target(self.target["/dev/nvme0n1"], "plain", None, progress_callback=progress):
                            self.fail("changed target was accepted")
                self.assertNotIn(forbidden_command, [command[0] for command in commands])

    def test_cleanup_tree_failure_is_reported_and_staging_is_retained(self) -> None:
        def runner(command: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
            if command[0] == "blkid":
                return _result(command, "11111111-1111-1111-1111-111111111111\n" if command[-1].endswith("p2") else "22222222-2222-2222-2222-222222222222\n")
            return _result(command)

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.multiple(
                disk_install,
                _run=mock.Mock(side_effect=runner),
                _preflight_error=mock.Mock(return_value=None),
                discover_disks=mock.Mock(return_value=[self.target["/dev/nvme0n1"]]),
                MOUNT_BASE=Path(temporary) / "mnt",
                SECRET_BASE=Path(temporary) / "run",
            ), mock.patch.object(disk_install.shutil, "rmtree", side_effect=OSError("busy")):
                with self.assertRaisesRegex(disk_install.InstallError, "cleanup failed; retained resources"):
                    with disk_install.prepare_target(self.target["/dev/nvme0n1"], "plain", None):
                        pass
            self.assertTrue(list((Path(temporary) / "mnt").glob("omarchy-pi-install-*")))

    def test_key_mode_keeps_recovery_secret_out_of_argv_and_cleans_secret_dir(self) -> None:
        commands: list[tuple[list[str], str | None]] = []
        progress: list[str] = []
        key = dict(self.blank_key)
        key["identity"] = dict(key["identity"])

        def runner(command: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
            commands.append((command, input_text))
            if command[0] == "blkid":
                device = command[-1]
                if device == "/dev/sdb1":
                    return _result(command, "33333333-3333-3333-3333-333333333333\n")
                if device == "/dev/nvme0n1p1":
                    return _result(command, "22222222-2222-2222-2222-222222222222\n")
                return _result(command, "11111111-1111-1111-1111-111111111111\n")
            if command[:2] == ["cryptsetup", "luksUUID"]:
                return _result(command, "44444444-4444-4444-4444-444444444444\n")
            return _result(command)

        with tempfile.TemporaryDirectory() as temporary:
            fake_key_stat = SimpleNamespace(st_size=64, st_mode=0o100400, st_uid=0, st_gid=0)
            with mock.patch.multiple(
                disk_install,
                _run=mock.Mock(side_effect=runner),
                _preflight_error=mock.Mock(return_value=None),
                discover_disks=mock.Mock(return_value=[self.target["/dev/nvme0n1"], key]),
                MOUNT_BASE=Path(temporary) / "mnt",
                SECRET_BASE=Path(temporary) / "run",
            ), mock.patch.object(disk_install.os, "chown", return_value=None), mock.patch.object(disk_install.Path, "stat", return_value=fake_key_stat):
                with disk_install.prepare_target(
                    self.target["/dev/nvme0n1"],
                    "key",
                    "recovery-passphrase",
                    key,
                    progress_callback=progress.append,
                ) as context:
                    self.assertEqual(context["key_uuid"], "33333333-3333-3333-3333-333333333333")
                    self.assertEqual(context["key_path"], "/.cryptroot.key")
                    self.assertEqual(context["luks_uuid"], "44444444-4444-4444-4444-444444444444")
            self.assertEqual(list((Path(temporary) / "run").glob("*")), [])

        cryptsetup_commands = [argv for argv, _ in commands if argv[0] == "cryptsetup"]
        self.assertTrue(any(argv[1] == "luksFormat" for argv in cryptsetup_commands))
        self.assertTrue(any(argv[1] == "luksAddKey" for argv in cryptsetup_commands))
        add_key = next(argv for argv in cryptsetup_commands if argv[1] == "luksAddKey")
        self.assertIn("--new-keyfile", add_key)
        self.assertNotIn("--new-key-file", add_key)
        for argv, _input in commands:
            self.assertNotIn("recovery-passphrase", argv)
        luks_format_input = next(value for argv, value in commands if argv[:2] == ["cryptsetup", "luksFormat"])
        self.assertEqual(luks_format_input, "recovery-passphrase")
        key_sfdisk = next(value for argv, value in commands if argv[0] == "sfdisk" and argv[-1] == "/dev/sdb")
        self.assertIn(",524288,83", key_sfdisk or "")
        wipefs_indices = [index for index, (argv, _input) in enumerate(commands) if argv[0] == "wipefs"]
        self.assertEqual(len(wipefs_indices), 2)
        for index in wipefs_indices:
            self.assertEqual(commands[index + 1][0], ["udevadm", "settle", "--timeout=30"])
        self.assertIn("Writing unlock-key USB partition table", progress)
        self.assertIn("Unlock-key USB partition table written", progress)
        self.assertIn("Formatting unlock-key USB filesystem (ext4)", progress)
        self.assertIn("Unlock key staged for target enrollment", progress)
        self.assertTrue(all("recovery-passphrase" not in label for label in progress))


if __name__ == "__main__":
    unittest.main()
