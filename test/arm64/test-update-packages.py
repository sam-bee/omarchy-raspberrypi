#!/usr/bin/python3
"""Focused tests for the packaged ARM Omarchy candidate contract."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from contextlib import redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import tarfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "install/arm64/update_packages.py"
SPEC = importlib.util.spec_from_file_location("omarchy_pi_update_packages_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
packages = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = packages
SPEC.loader.exec_module(packages)


class CandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        (self.source / "migrations").mkdir(parents=True)
        (self.source / "install/arm64").mkdir(parents=True)
        (self.source / "install/arm64/migrations.allowlist").write_text("# reviewed\n", encoding="utf-8")
        self.revision = "a" * 40
        (self.source / ".omarchy-pi-source-commit").write_text(self.revision + "\n", encoding="utf-8")

    def _source_archive(self) -> tuple[Path, str]:
        archive = self.root / "source.tar"
        with tarfile.open(archive, "w") as stream:
            for path in sorted(self.source.rglob("*")):
                if path.name == ".omarchy-pi-source-commit":
                    continue
                stream.add(path, arcname=path.relative_to(self.source).as_posix(), recursive=False)
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _archive(self, directory: str, name: str, content: str) -> dict[str, str]:
        path = self.root / directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {
            "name": name.split("-", 1)[0],
            "version": "1.0-1",
            "architecture": "aarch64",
            "filename": f"{directory}/{name}",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "signature": "optional",
        }

    def _manifest(self, *, names=("omarchy", "omarchy-settings"), previous=True) -> None:
        source_archive, source_sha256 = self._source_archive()
        channel = "dev" if names[0] == "omarchy-dev" else "stable"
        packages = []
        for name in names:
            filename = f"{name}-1.0-1-aarch64.pkg.tar.zst"
            path = self.root / "packages" / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            self._native_archive(path, name, "1.0-1", self.revision, source_sha256, channel)
            packages.append({
                "name": name,
                "version": "1.0-1",
                "architecture": "aarch64",
                "filename": f"packages/{filename}",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "signature": "optional",
            })
        rollback = []
        if previous:
            for name in names:
                filename = f"{name}-0.9-1-aarch64.pkg.tar.zst"
                path = self.root / "previous" / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                self._native_archive(path, name, "0.9-1", "b" * 40, "c" * 64, channel)
                rollback.append({
                    "name": name,
                    "version": "0.9-1",
                    "architecture": "aarch64",
                    "filename": f"previous/{filename}",
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "signature": "optional",
                })
        (self.root / "candidate.json").write_text(json.dumps({
            "schema_version": 1,
            "architecture": "aarch64",
            "channel": channel,
            "source_revision": self.revision,
            "source": {"tree": "source", "archive": source_archive.name, "archive_sha256": source_sha256},
            "packages": packages,
            "previous_packages": rollback,
        }), encoding="utf-8")

    def _native_archive(self, path: Path, name: str, version: str, revision: str, source_sha256: str, channel: str) -> None:
        settings = "omarchy-settings-dev" if channel == "dev" else "omarchy-settings"
        metadata = [
            f"pkgname = {name}",
            f"pkgver = {version}",
            "arch = aarch64",
        ]
        if name == ("omarchy-dev" if channel == "dev" else "omarchy"):
            metadata.append(f"depend = {settings}={version}")
        marker = json.dumps({
            "schema_version": 1,
            "layout": "packaged",
            "runtime_mode": "packaged",
            "channel": channel,
            "version": version,
            "source_revision": revision,
            "source_sha256": source_sha256,
        }, sort_keys=True).encode() + b"\n"
        with tarfile.open(path, "w") as stream:
            def add(name_: str, data: bytes) -> None:
                info = tarfile.TarInfo(name_)
                info.size = len(data)
                info.mode = 0o644
                info.mtime = 0
                stream.addfile(info, io.BytesIO(data))
            add(".PKGINFO", ("\n".join(metadata) + "\n").encode())
            add("usr/share/omarchy/.omarchy-pi-source-commit", (revision + "\n").encode())
            add("usr/share/omarchy/.omarchy-pi-packaged.json", marker)
            if name in {"omarchy", "omarchy-dev"}:
                add("usr/share/omarchy/version", version.split("-", 1)[0].encode() + b"\n")
            else:
                add("usr/share/omarchy/config/test.conf", b"test=true\n")

    def test_candidate_requires_matching_pair_and_migrations(self) -> None:
        self._manifest()
        candidate = packages.load_candidate(self.root)
        self.assertEqual(candidate.package_names, ("omarchy", "omarchy-settings"))
        self.assertEqual(candidate.source_revision, self.revision)
        self.assertTrue(packages.needs_optional_local_signature(candidate))

        (self.root / "candidate.json").write_text(
            (self.root / "candidate.json").read_text().replace('"omarchy-settings"', '"omarchy-settings-dev"'),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(packages.CandidateError, "matching Omarchy"):
            packages.load_candidate(self.root)

    def test_candidate_requires_rollback_pair(self) -> None:
        self._manifest(previous=False)
        with self.assertRaisesRegex(packages.CandidateError, "previous package archives"):
            packages.load_candidate(self.root)

    def test_source_tree_must_match_deterministic_archive(self) -> None:
        self._manifest()
        (self.source / "install/arm64/migrations.allowlist").write_text("# changed\n", encoding="utf-8")
        with self.assertRaisesRegex(packages.CandidateError, "differs from its deterministic Git archive"):
            packages.load_candidate(self.root)

    def test_builder_pair_manifest_is_accepted_with_explicit_rollback_records(self) -> None:
        self._manifest()
        document = json.loads((self.root / "candidate.json").read_text())
        pair = {
            "schema_version": 1,
            "architecture": "aarch64",
            "version": "1.0-1",
            "source": {"revision": self.revision, "archive": "source.tar", "archive_sha256": document["source"]["archive_sha256"]},
            "packages": {
                package["name"]: {"filename": package["filename"], "sha256": package["sha256"]}
                for package in document["packages"]
            },
        }
        (self.root / "candidate.json").unlink()
        (self.root / "pair.json").write_text(json.dumps(pair), encoding="utf-8")
        (self.root / "previous.json").write_text(json.dumps(document["previous_packages"]), encoding="utf-8")
        candidate = packages.load_candidate(self.root)
        self.assertEqual(candidate.manifest.name, "pair.json")
        self.assertEqual(candidate.package_names, ("omarchy", "omarchy-settings"))

    def test_builder_dev_pair_uses_matching_dev_package_names(self) -> None:
        self._manifest(names=("omarchy-dev", "omarchy-settings-dev"))
        document = json.loads((self.root / "candidate.json").read_text())
        pair = {
            "schema_version": 1,
            "architecture": "aarch64",
            "version": "1.0-1",
            "channel": "dev",
            "source": {"revision": self.revision, "archive": "source.tar", "archive_sha256": document["source"]["archive_sha256"]},
            "packages": {
                package["name"]: {"filename": package["filename"], "sha256": package["sha256"]}
                for package in document["packages"]
            },
        }
        (self.root / "candidate.json").unlink()
        (self.root / "pair.json").write_text(json.dumps(pair), encoding="utf-8")
        (self.root / "previous.json").write_text(json.dumps(document["previous_packages"]), encoding="utf-8")
        candidate = packages.load_candidate(self.root)
        self.assertEqual(candidate.channel, "dev")
        self.assertEqual(candidate.package_names, ("omarchy-dev", "omarchy-settings-dev"))

    def test_builder_archive_manifest_is_accepted(self) -> None:
        self._manifest()
        document = json.loads((self.root / "candidate.json").read_text())
        archives = [
            {
                "package": package["name"],
                "version": package["version"],
                "architecture": package["architecture"],
                "source_revision": self.revision,
                "source_sha256": document["source"]["archive_sha256"],
                "package_sha256": package["sha256"],
                "package_signature": "unsigned",
                "filename": package["filename"],
            }
            for package in document["packages"]
        ]
        (self.root / "candidate.json").unlink()
        (self.root / "manifest.json").write_text(json.dumps({
            "schema_version": 1,
            "architecture": "aarch64",
            "version": "1.0-1",
            "source_revision": self.revision,
            "source_sha256": document["source"]["archive_sha256"],
            "source": {"tree": "source", "archive": "source.tar", "archive_sha256": document["source"]["archive_sha256"]},
            "archives": archives,
        }), encoding="utf-8")
        (self.root / "previous.json").write_text(json.dumps(document["previous_packages"]), encoding="utf-8")
        candidate = packages.load_candidate(self.root)
        self.assertEqual(candidate.manifest.name, "manifest.json")
        self.assertEqual(candidate.source_sha256, document["source"]["archive_sha256"])

    def test_signed_candidate_requires_detached_signatures_without_relaxing_policy(self) -> None:
        self._manifest()
        document = json.loads((self.root / "candidate.json").read_text())
        for record in document["packages"]:
            signature = record["filename"] + ".sig"
            (self.root / signature).write_text("reviewed signature\n", encoding="utf-8")
            record["signature"] = "required"
            record["signature_file"] = signature
        (self.root / "candidate.json").write_text(json.dumps(document), encoding="utf-8")
        candidate = packages.load_candidate(self.root)
        self.assertTrue(all(package.signature_file is not None for package in candidate.packages))
        self.assertFalse(packages.needs_optional_local_signature(candidate))

    def test_local_transaction_is_same_reviewed_command_for_upgrade_or_downgrade(self) -> None:
        self._manifest()
        candidate = packages.load_candidate(self.root)
        self.assertEqual(packages.local_transaction_command(candidate)[1], "-U")
        self.assertNotIn("--needed", packages.local_transaction_command(candidate, force=True))
        with mock.patch.object(packages.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="-1\n")):
            self.assertEqual(packages.candidate_actions(candidate, {"omarchy": "2.0-1", "omarchy-settings": "2.0-1"}), {
                "omarchy": "upgrade",
                "omarchy-settings": "upgrade",
            })

    def test_current_pair_requires_matching_versions_and_source_marker(self) -> None:
        self._manifest()
        candidate = packages.load_candidate(self.root)
        installed = {name: "1.0-1" for name in candidate.package_names}
        self.assertTrue(packages.pair_is_current(candidate, installed, self.revision))
        self.assertFalse(packages.pair_is_current(candidate, {**installed, "omarchy": "0.9-1"}, self.revision))
        self.assertFalse(packages.pair_is_current(candidate, installed, "b" * 40))
        retained = tuple(candidate.packages)
        self.assertTrue(packages.pair_is_current(candidate, installed, self.revision, retained))
        changed = list(retained)
        changed[0] = packages.CandidatePackage(
            changed[0].name, changed[0].version, changed[0].architecture,
            changed[0].archive, "c" * 64, changed[0].signature, changed[0].signature_file,
        )
        self.assertFalse(packages.pair_is_current(candidate, installed, self.revision, changed))
        with mock.patch.object(packages.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="1\n")):
            self.assertEqual(packages.candidate_actions(candidate, {"omarchy": "0.8-1", "omarchy-settings": "0.8-1"}), {
                "omarchy": "downgrade",
                "omarchy-settings": "downgrade",
            })

    def test_same_source_marker_reports_a_candidate_noop(self) -> None:
        self._manifest()
        runtime = self.root / "installed"
        runtime.mkdir()
        (runtime / packages.PACKAGED_MARKER).write_text(json.dumps({
            "schema_version": 1,
            "runtime_mode": "packaged",
            "source_revision": self.revision,
        }), encoding="utf-8")
        output = io.StringIO()
        with mock.patch.dict(packages.os.environ, {
            "OMARCHY_PI_TESTING": "1",
            "OMARCHY_PI_TEST_RUNTIME_ROOT": str(runtime),
        }, clear=False), redirect_stdout(output):
            with mock.patch.object(packages, "installed_package_versions", return_value={
                "omarchy": "1.0-1", "omarchy-settings": "1.0-1",
            }), mock.patch.object(packages, "load_installed_rollback", return_value=()):
                status = packages.main(["check", "--root", str(self.root)])
        self.assertEqual(status, 1)
        self.assertIn("current", output.getvalue())

    def test_package_provenance_requires_compatible_pair(self) -> None:
        runtime = self.root / "usr/share/omarchy"
        (runtime / "config").mkdir(parents=True)
        (runtime / "version").write_text("4\n", encoding="utf-8")
        (runtime / packages.PACKAGED_MARKER).write_text(json.dumps({"schema_version": 1, "runtime_mode": "packaged", "channel": "stable", "source_revision": "a" * 40}), encoding="utf-8")

        def owner(command, **kwargs):
            path = command[-1]
            package = "omarchy" if path.endswith("version") else "omarchy-settings"
            return mock.Mock(returncode=0, stdout=f"{package} 4-1 owns {path}\n")

        with mock.patch.object(packages, "RUNTIME_ROOT", runtime):
            self.assertEqual(packages.package_provenance(runtime, runner=owner), "stable")

    def test_installed_rollback_loads_and_hashes_pair_outside_candidate(self) -> None:
        rollback = self.root / "rollback"
        rollback.mkdir()
        records = []
        for name in ("omarchy", "omarchy-settings"):
            archive = rollback / f"{name}-0.9-1-aarch64.pkg.tar.zst"
            self._native_archive(archive, name, "0.9-1", "b" * 40, "c" * 64, "stable")
            records.append({
                "package": name,
                "filename": archive.name,
                "package_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            })
        (rollback / "manifest.json").write_text(json.dumps({
            "schema_version": 1,
            "architecture": "aarch64",
            "version": "0.9-1",
            "packages": records,
        }), encoding="utf-8")

        retained = packages.load_installed_rollback(
            ("omarchy", "omarchy-settings"), roots=(rollback,)
        )
        self.assertEqual({package.name for package in retained}, {"omarchy", "omarchy-settings"})
        self.assertEqual({package.version for package in retained}, {"0.9-1"})

        (rollback / records[0]["filename"]).write_bytes(b"changed")
        with self.assertRaisesRegex(packages.CandidateError, "rollback"):
            packages.load_installed_rollback(("omarchy", "omarchy-settings"), roots=(rollback,))


if __name__ == "__main__":
    unittest.main(verbosity=2)
