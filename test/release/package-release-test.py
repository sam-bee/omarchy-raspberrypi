#!/usr/bin/env python3
"""Focused acceptance-schema tests for the release packager."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "release/package-release.py"
SPEC = importlib.util.spec_from_file_location("omarchy_pi_release", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)


class NativeAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.image_bytes = b"final raw image fixture\n"
        self.image_sha = hashlib.sha256(self.image_bytes).hexdigest()
        self.image = self.root / "image.img"
        self.image.write_bytes(self.image_bytes)
        self.installer_revision = "a" * 40
        self.runtime_sha = "b" * 64

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _check(self, document: dict) -> tuple[dict, str, dict]:
        path = self.root / "acceptance.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return release._native_acceptance(
            path,
            image_digest=release.Digest(len(self.image_bytes), self.image_sha),
            installer_revision=self.installer_revision,
            installer_runtime_sha=self.runtime_sha,
        )

    def test_exact_image_native_boot_remains_accepted(self) -> None:
        _, scope, flags = self._check({
            "status": "passed",
            "raw_sha256": self.image_sha,
            "native_boot_tested": True,
            "scope": ["exact-image-native-boot", "runtime-rdp"],
        })
        self.assertEqual(scope, "exact-image-native-boot, runtime-rdp")
        self.assertTrue(flags["native_boot_tested"])

    def test_inherited_baseline_and_final_runtime_file_checks_are_accepted(self) -> None:
        _, scope, flags = self._check({
            "status": "passed",
            "raw_sha256": self.image_sha,
            "scope": [
                "inherited-baseline",
                "native-runtime-verified",
                "file-image-verified",
                "normal-update-reboot-rdp",
            ],
            "validation": {
                "native_boot_tested": False,
                "native_runtime_verified": True,
                "file_image_verified": True,
                "inherited_baseline": True,
            },
        })
        self.assertIn("inherited-baseline", scope)
        self.assertFalse(flags["native_boot_tested"])
        self.assertTrue(flags["native_runtime_verified"])
        self.assertTrue(flags["file_image_verified"])

    def test_false_native_boot_without_final_checks_is_rejected(self) -> None:
        with self.assertRaisesRegex(release.ReleaseError, "requires inherited baseline"):
            self._check({
                "status": "passed",
                "raw_sha256": self.image_sha,
                "native_boot_tested": False,
                "scope": "file checks only",
            })

    def test_incorrect_status_or_image_binding_is_rejected(self) -> None:
        with self.assertRaisesRegex(release.ReleaseError, "status must be passed"):
            self._check({
                "status": "accepted",
                "raw_sha256": self.image_sha,
                "native_boot_tested": True,
                "scope": "exact-image-native-boot",
            })
        with self.assertRaisesRegex(release.ReleaseError, "does not match"):
            self._check({
                "status": "passed",
                "raw_sha256": "c" * 64,
                "native_boot_tested": True,
                "scope": "exact-image-native-boot",
            })

    def test_release_text_reports_false_exact_image_boot(self) -> None:
        text = release._release_text(
            "test", "image", release.Digest(1, self.image_sha), release.Digest(1, "d" * 64),
            [release.Digest(1, "e" * 64)], self.installer_revision, self.runtime_sha,
            "f" * 40, release.Digest(1, "0" * 64), [], [],
            {"status": "passed"}, "inherited-baseline, file-image-verified",
            {"native_boot_tested": False},
        )
        self.assertIn("exact-image native boot tested: `false`", text)

    def test_build_manifest_derives_native_boot_from_acceptance(self) -> None:
        candidate = self.root / "candidate"
        candidate.mkdir()
        (candidate / "image.img").write_bytes(self.image_bytes)
        (candidate / "output-image.json").write_text(json.dumps({
            "raw": {"name": "image.img", "bytes": len(self.image_bytes), "sha256": self.image_sha},
            "installer": {"source_revision": self.installer_revision, "runtime_sha256": self.runtime_sha},
            "desktop": {"source_revision": "f" * 40},
        }), encoding="utf-8")
        (candidate / "provenance.json").write_text(json.dumps({
            "installer": {"source_revision": self.installer_revision},
        }), encoding="utf-8")
        (candidate / "build-receipt.json").write_text(json.dumps({"validation": {}}), encoding="utf-8")
        desktop_archive = candidate / "desktop.tar.zst"
        desktop_archive.write_bytes(b"desktop fixture")
        installer_manifest = self.root / "installer.json"
        installer_manifest.write_text(json.dumps({
            "transaction": {"resolved_packages": [{"name": "linux-rpi", "version": "1", "repository": "core"}]},
        }), encoding="utf-8")
        acceptance = self.root / "acceptance.json"
        acceptance.write_text(json.dumps({
            "status": "passed",
            "raw_sha256": self.image_sha,
            "scope": ["inherited-baseline", "native-runtime-verified", "file-image-verified"],
            "validation": {
                "native_boot_tested": False,
                "native_runtime_verified": True,
                "file_image_verified": True,
                "inherited_baseline": True,
            },
        }), encoding="utf-8")
        license_file = self.root / "LICENSES.md"
        license_file.write_text("licenses\n", encoding="utf-8")
        args = argparse.Namespace(
            release="test", candidate_dir=candidate, output_dir=self.root / "release",
            installer_package_manifest=installer_manifest, native_acceptance=acceptance,
            desktop_archive=desktop_archive, image_stem="image", licenses_source=license_file,
        )
        desktop_manifest = {
            "transaction": {"installed_packages": [{"name": "base", "version": "1", "repository": "core"}]},
            "runtime": {"packages": [
                {"name": "omarchy", "version": "4.0-1", "architecture": "aarch64"},
                {"name": "omarchy-settings", "version": "4.0-1", "architecture": "aarch64"},
            ]},
        }
        with mock.patch.object(release, "_extract_desktop_manifest", return_value=(desktop_manifest, b"desktop manifest")), \
             mock.patch.object(release, "_compress_and_split", return_value=(release.Digest(2, "d" * 64), [release.Digest(1, "e" * 64)])), \
             mock.patch.object(release, "_sum_lines", return_value="fixture\n"):
            output = release.build(args)

        manifest = json.loads((output / "release-manifest.json").read_text(encoding="utf-8"))
        verification = manifest["verification"]
        self.assertFalse(verification["native_boot_tested"])
        self.assertTrue(verification["native_runtime_verified"])
        self.assertTrue(verification["file_image_verified"])
        self.assertTrue(verification["inherited_baseline"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
