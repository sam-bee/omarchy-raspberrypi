#!/usr/bin/python3
"""Focused checks for the packaged runtime input boundary."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


MODULE = Path(__file__).with_name("build-desktop-payload.py")
SPEC = importlib.util.spec_from_file_location("build_desktop_payload", MODULE)
assert SPEC and SPEC.loader
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class RuntimeInputTests(unittest.TestCase):
    def document(self, revision: str = "a" * 40) -> dict:
        records = []
        for package in ("omarchy-settings", "omarchy"):
            records.append(
                {
                    "package": package,
                    "version": "1.0-1",
                    "architecture": "aarch64",
                    "filename": f"{package}-1.0-1-aarch64.pkg.tar.zst",
                    "package_sha256": "b" * 64,
                    "source_revision": revision,
                    "source_sha256": "c" * 64,
                    "package_signature": "unsigned",
                    "signature": None,
                    "signature_sha256": None,
                    "files": [f"usr/share/omarchy/{package}"],
                }
            )
        return {"schema_version": 1, "source_revision": revision, "source_sha256": "c" * 64, "packages": records}

    def test_builder_manifest_is_normalized_as_a_matching_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            path.write_text(json.dumps(self.document()), encoding="utf-8")
            records = builder._runtime_package_records(path, source_revision="a" * 40)
            self.assertEqual(tuple(records), ("omarchy-settings", "omarchy"))
            self.assertEqual(records["omarchy"]["sha256"], "b" * 64)

    def test_stale_source_and_incomplete_pair_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            stale = self.document("d" * 40)
            path.write_text(json.dumps(stale), encoding="utf-8")
            with self.assertRaisesRegex(builder.DesktopPayloadError, "source revision"):
                builder._runtime_package_records(path, source_revision="a" * 40)
            incomplete = self.document()
            incomplete["packages"] = incomplete["packages"][:1]
            path.write_text(json.dumps(incomplete), encoding="utf-8")
            with self.assertRaisesRegex(builder.DesktopPayloadError, "runtime package manifest"):
                builder._runtime_package_records(path, source_revision="a" * 40)


if __name__ == "__main__":
    unittest.main()
