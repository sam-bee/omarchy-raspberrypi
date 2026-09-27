#!/usr/bin/python3
"""Focused tests for the safe image-build orchestrator."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("build_installer_image", HERE / "build-installer-image.py")
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BuildInstallerImageTests(unittest.TestCase):
    def make_inputs(self, directory: Path) -> dict[str, object]:
        archive = directory / "rootfs.tar.zst"
        signature = directory / "rootfs.tar.zst.sig"
        keyring = directory / "archlinuxarm.gpg"
        binary = directory / "hypr-rdp"
        archive.write_bytes(b"signed rootfs archive")
        signature.write_bytes(b"detached signature")
        keyring.write_bytes(b"trusted keyring")
        binary.write_bytes(b"aarch64 hypr-rdp")
        return {
            "archive": archive,
            "signature": signature,
            "keyring": keyring,
            "signer_fingerprint": "A" * 40,
            "hypr_rdp": binary,
            "hypr_rdp_sha256": digest(binary),
            "archive_sha256": digest(archive),
            "repo_server": "https://mirror.archlinuxarm.org/$arch/$repo",
        }

    def call_plan(self, inputs: dict[str, object], directory: Path, *, workdir: Path | None = None, output: Path | None = None):
        return MODULE.plan(
            **inputs,
            workdir=workdir or directory / "work",
            output=output or directory / "installer.img",
        )

    def test_plan_records_inputs_without_creating_build_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            inputs = self.make_inputs(directory)
            workdir = directory / "work"
            output = directory / "installer.img"
            document = self.call_plan(inputs, directory, workdir=workdir, output=output)

            self.assertEqual(document["mode"], "plan")
            self.assertEqual(document["inputs"]["archive"]["sha256"], inputs["archive_sha256"])
            self.assertEqual(document["inputs"]["hypr_rdp"]["sha256"], inputs["hypr_rdp_sha256"])
            self.assertTrue(document["apply_required"])
            self.assertFalse(workdir.exists())
            self.assertFalse(output.exists())

    def test_plan_rejects_mismatched_hashes_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            inputs = self.make_inputs(directory)
            inputs["archive_sha256"] = "0" * 64
            with self.assertRaisesRegex(MODULE.InstallerBuildError, "archive SHA-256"):
                self.call_plan(inputs, directory)
            self.assertFalse((directory / "work").exists())

            inputs = self.make_inputs(directory)
            inputs["hypr_rdp_sha256"] = "0" * 64
            with self.assertRaisesRegex(MODULE.InstallerBuildError, "hypr-rdp binary SHA-256"):
                self.call_plan(inputs, directory, workdir=directory / "work-2")

    def test_plan_requires_fresh_workdir_and_output_outside_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            inputs = self.make_inputs(directory)
            existing = directory / "existing-work"
            existing.mkdir()
            with self.assertRaisesRegex(MODULE.InstallerBuildError, "choose a new path"):
                self.call_plan(inputs, directory, workdir=existing)

            with self.assertRaisesRegex(MODULE.InstallerBuildError, "outside the build work directory"):
                self.call_plan(inputs, directory, workdir=directory / "fresh-work", output=directory / "fresh-work" / "image.img")

            redirected = directory / "redirected"
            redirected.mkdir()
            (directory / "work-link").symlink_to(redirected, target_is_directory=True)
            with self.assertRaisesRegex(MODULE.InstallerBuildError, "symlink path component"):
                self.call_plan(inputs, directory, workdir=directory / "work-link" / "new-work")

    def test_apply_calls_components_in_order_and_records_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            inputs = self.make_inputs(directory)
            workdir = directory / "work"
            output = directory / "installer.img"
            calls: list[str] = []

            class FakeRootfs:
                @staticmethod
                def prepare_rootfs(archive, signature, keyring, fingerprint, prepared, *, expected_archive_sha256):
                    calls.append("rootfs")
                    rootfs = prepared / "rootfs"
                    rootfs.mkdir(parents=True)
                    (prepared / "rootfs-manifest.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
                    return {"source": {"archive_sha256": expected_archive_sha256}, "staging": {"rootfs_directory": "rootfs"}}

            class FakePackages:
                @staticmethod
                def stage_packages(rootfs, rootfs_manifest, package_manifest, *, apply, repo_server):
                    calls.append("packages")
                    package_manifest.write_text('{"schema_version": 1, "mode": "apply"}\n', encoding="utf-8")
                    return {"schema_version": 1, "mode": "apply"}

            class FakeBoot:
                @staticmethod
                def configure_installer_boot(rootfs, *, generate):
                    calls.append("boot")
                    return {"generated": generate}

            class FakeServices:
                @staticmethod
                def stage_services(rootfs, binary, expected_sha256):
                    calls.append("services")
                    return {"binary_sha256": expected_sha256}

            class FakeImage:
                @staticmethod
                def assemble_image(rootfs, image, *, mkfs_fat):
                    calls.append("image")
                    self.assertEqual(mkfs_fat, rootfs / "usr/bin/mkfs.fat")
                    image.write_bytes(b"regular image")
                    return {"output": str(image)}

            fake_components = {
                "rootfs": FakeRootfs,
                "packages": FakePackages,
                "boot": FakeBoot,
                "services": FakeServices,
                "image": FakeImage,
            }
            with patch.object(MODULE.platform, "machine", return_value="aarch64"), \
                 patch.object(MODULE.os, "geteuid", return_value=0), \
                 patch.object(MODULE, "_components", return_value=fake_components):
                result = MODULE.build(
                    **inputs,
                    workdir=workdir,
                    output=output,
                )

            self.assertEqual(calls, ["rootfs", "packages", "boot", "services", "image"])
            self.assertEqual(result["mode"], "apply")
            self.assertEqual(result["artifacts"]["image_sha256"], digest(output))
            manifest = json.loads((workdir / "build-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["steps"]["packages"]["mode"], "apply")
            self.assertEqual(manifest["artifacts"]["image_bytes"], len(b"regular image"))

    def test_cli_has_no_block_device_or_private_settings_argument(self) -> None:
        parser = MODULE.build_parser()
        options = {action.dest for action in parser._actions}
        self.assertNotIn("device", options)
        self.assertNotIn("settings", options)


if __name__ == "__main__":
    unittest.main()
