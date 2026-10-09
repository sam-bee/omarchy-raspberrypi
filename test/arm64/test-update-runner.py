#!/usr/bin/python3
"""Focused contract tests for the durable Pi update runner."""

from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import stat
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "install/arm64/update.py"
SPEC = importlib.util.spec_from_file_location("omarchy_pi_update_runner", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update)


class UpdateRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.task_home = self.base / "home"
        self.task_home.mkdir()
        self.account = SimpleNamespace(pw_name="sierra", pw_uid=1000, pw_gid=1000, pw_dir=str(self.task_home))
        self.release_sha = "a" * 40
        self.release = self.task_home / ".local/share/omarchy-pi/releases" / self.release_sha
        self.release.mkdir(parents=True)
        (self.release / ".omarchy-pi-source-commit").write_text(self.release_sha + "\n", encoding="utf-8")
        (self.release.parent.parent / "current").symlink_to(Path("releases") / self.release_sha)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _launch_patches(self, commands, root_calls):
        state_root = self.base / "state" / "updates"

        def test_root_directory(path, mode=0o755, gid=0):
            path = Path(path)
            root_calls.append((path, mode, gid))
            path.mkdir(mode=mode, parents=True, exist_ok=True)
            path.chmod(mode)

        def fake_command(argv, **kwargs):
            commands.append(list(argv))
            if argv[:3] == ["/usr/bin/systemctl", "show", update.UNIT]:
                return SimpleNamespace(stdout="inactive\n", returncode=0)
            return SimpleNamespace(stdout="", returncode=0)

        def fake_subprocess_run(argv, **kwargs):
            commands.append(list(argv))
            return SimpleNamespace(stdout="", returncode=0)

        stack = ExitStack()
        stack.enter_context(mock.patch.object(update, "STATE_ROOT", state_root))
        stack.enter_context(mock.patch.object(update, "require_pi"))
        stack.enter_context(mock.patch.object(update, "active_release", return_value=self.release))
        stack.enter_context(mock.patch.object(update, "command", side_effect=fake_command))
        stack.enter_context(mock.patch.object(update, "root_directory", side_effect=test_root_directory))
        stack.enter_context(mock.patch.object(update.pwd, "getpwuid", return_value=self.account))
        stack.enter_context(mock.patch.object(update.os, "geteuid", return_value=0))
        stack.enter_context(mock.patch.object(update.os, "chown"))
        stack.enter_context(mock.patch.object(update.subprocess, "run", side_effect=fake_subprocess_run))
        return stack

    def _native_archive(self, path, name, version, revision):
        marker = json.dumps({
            "schema_version": 1,
            "layout": "packaged",
            "runtime_mode": "packaged",
            "channel": "stable",
            "version": version,
            "source_revision": revision,
            "source_sha256": "c" * 64,
        }, sort_keys=True).encode() + b"\n"
        with tarfile.open(path, "w") as stream:
            def add(name_, data):
                info = tarfile.TarInfo(name_)
                info.size = len(data)
                info.mode = 0o644
                stream.addfile(info, io.BytesIO(data))
            metadata = f"pkgname = {name}\npkgver = {version}\narch = aarch64\n"
            if name == "omarchy":
                metadata += f"depend = omarchy-settings={version}\n"
            add(".PKGINFO", metadata.encode())
            add("usr/share/omarchy/.omarchy-pi-source-commit", (revision + "\n").encode())
            add("usr/share/omarchy/.omarchy-pi-packaged.json", marker)

    def test_launch_copies_private_runtime_and_starts_durable_pid1_worker(self):
        commands = []
        root_calls = []
        with mock.patch.dict(update.os.environ, {"SUDO_UID": str(self.account.pw_uid)}, clear=False), self._launch_patches(commands, root_calls):
            output = io.StringIO()
            with redirect_stdout(output):
                update.launch()

        response = json.loads(output.getvalue())
        job = Path(response["job"])
        runtime = job / "runtime"
        self.assertEqual(response["status"], "queued")
        self.assertEqual(json.loads((job / "result.json").read_text())["status"], "queued")
        request = json.loads((job / "request.json").read_text())
        self.assertEqual(request["username"], self.account.pw_name)
        self.assertEqual(request["old_release"], str(self.release))
        self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o755)
        for name in update.RUNTIME_FILES:
            self.assertEqual(stat.S_IMODE((runtime / name).stat().st_mode), 0o644, name)

        start = next(command for command in commands if command[0] == "/usr/bin/systemd-run")
        self.assertIn("--unit=" + update.UNIT, start)
        self.assertIn("--property=Type=exec", start)
        self.assertIn("--property=UMask=0027", start)
        self.assertIn("--collect", start)
        self.assertEqual(start[-5:], ["/usr/bin/python3", "-I", str(runtime / "update.py"), "--worker", str(job)])
        reset = ["/usr/bin/systemctl", "reset-failed", update.UNIT]
        self.assertLess(commands.index(reset), commands.index(start))

    def test_packaged_launch_records_candidate_pair_for_durable_worker(self):
        commands = []
        root_calls = []
        candidate_root = self.base / "candidate"
        candidate_root.mkdir()
        (candidate_root / "candidate.json").write_text("{}\n", encoding="utf-8")
        (candidate_root / "source").mkdir(mode=0o700)
        (candidate_root / "source/migration.sh").write_text("#!/bin/bash\n", encoding="utf-8")
        candidate = SimpleNamespace(
            root=candidate_root,
            manifest=candidate_root / "candidate.json",
            channel="stable",
            source_tree=candidate_root / "source",
            packages=(),
            previous_packages=(),
        )
        def load_candidate(root, *, require_previous):
            root = Path(root)
            if root == candidate_root:
                return candidate
            return SimpleNamespace(
                root=root,
                manifest=root / "candidate.json",
                channel="stable",
                source_tree=root / "source",
                packages=(),
                previous_packages=(),
            )
        with mock.patch.dict(update.os.environ, {"SUDO_UID": str(self.account.pw_uid)}, clear=False), self._launch_patches(commands, root_calls), \
             mock.patch.object(update, "require_packaged_runtime", return_value="stable"), \
             mock.patch.object(update, "PACKAGED_RUNTIME_ROOT", ROOT), \
             mock.patch.object(update.update_packages, "load_candidate", side_effect=load_candidate):
            output = io.StringIO()
            with redirect_stdout(output):
                update.launch(packaged=True, candidate_root=candidate_root)

        response = json.loads(output.getvalue())
        request = json.loads((Path(response["job"]) / "request.json").read_text())
        self.assertEqual(request["mode"], "packaged")
        self.assertEqual(request["channel"], "stable")
        self.assertEqual(Path(request["candidate_root"]).name, "candidate")
        self.assertEqual(Path(request["candidate_root"]).parent.parent, self.base / "state/updates")
        self.assertEqual(request["candidate_manifest"], str(Path(request["candidate_root"]) / "candidate.json"))
        snapshot_source = Path(request["candidate_root"]) / "source"
        self.assertTrue(stat.S_IMODE(snapshot_source.stat().st_mode) & 0o050)

    def test_packaged_candidate_is_prepared_as_desktop_user_from_pinned_checkout(self):
        revision = "b" * 40
        checkout = self.task_home / ".cache/omarchy-pi/checkouts" / revision
        (checkout / "install/arm64").mkdir(parents=True)
        (checkout / "migrations").mkdir()
        (checkout / "install/arm64/migrations.allowlist").write_text("# reviewed\n", encoding="utf-8")
        builder = checkout / "install/arm64/build-runtime-packages.py"
        builder.write_text("# pinned builder\n", encoding="utf-8")
        runtime = self.base / "packaged-runtime/install/arm64"
        runtime.mkdir(parents=True)
        (runtime / "update-source.py").write_text("# pinned source updater\n", encoding="utf-8")
        candidate_root = self.task_home / ".cache/omarchy-pi/runtime-candidates" / revision
        candidate = SimpleNamespace(root=candidate_root, source_revision=revision)
        commands = []

        def prepare(argv, *, home):
            commands.append((list(argv), Path(home)))
            if "update-source.py" in argv[1]:
                return SimpleNamespace(stdout=json.dumps({"revision": revision, "checkout": str(checkout)}))
            candidate_root.mkdir(parents=True)
            return SimpleNamespace(stdout="")

        with mock.patch.object(update, "require_packaged_runtime", return_value="stable"), \
             mock.patch.object(update.pwd, "getpwuid", return_value=self.account), \
             mock.patch.object(update, "PACKAGED_RUNTIME_ROOT", runtime.parent.parent), \
             mock.patch.dict(update.os.environ, {"OMARCHY_PATH": str(runtime.parent.parent)}, clear=False), \
             mock.patch.object(update, "_prepare_packaged_command", side_effect=prepare), \
             mock.patch.object(update, "_prepare_packaged_source_archive", side_effect=lambda checkout, revision, destination, home: Path(destination).write_bytes(b"archive")), \
             mock.patch.object(update.update_packages, "_pair_document", return_value={
                 "schema_version": 1,
                 "architecture": "aarch64",
                 "channel": "stable",
                 "source_revision": revision,
                 "source": {"tree": "source", "archive": "source.tar", "archive_sha256": update.update_packages.hashlib.sha256(b"archive").hexdigest()},
                 "packages": [],
             }), \
             mock.patch.object(update.update_packages, "load_candidate", return_value=candidate):
            result = update.prepare_packaged_candidate()

        self.assertEqual(result, candidate_root)
        self.assertEqual(len(commands), 2)
        self.assertIn("prepare", commands[0][0])
        self.assertEqual(commands[1][0][1], str(builder))
        self.assertEqual(commands[1][1], self.task_home)
        self.assertTrue((candidate_root / "source/install/arm64/build-runtime-packages.py").is_file())

    def test_packaged_worker_publishes_new_pair_for_next_offline_rollback(self):
        rollback = self.base / "usr/share/omarchy-pi/rollback"
        rollback.parent.mkdir(parents=True)
        packages = []
        for name in ("omarchy", "omarchy-settings"):
            archive = self.base / f"{name}-2.0-1-aarch64.pkg.tar.zst"
            self._native_archive(archive, name, "2.0-1", "b" * 40)
            packages.append(update.update_packages.CandidatePackage(
                name=name,
                version="2.0-1",
                architecture="aarch64",
                archive=archive,
                sha256=update.update_packages._digest(archive),
                signature="optional",
            ))
        candidate = SimpleNamespace(packages=tuple(packages), source_revision="b" * 40)
        def permissive_root_directory(path, mode=0o755, gid=0):
            Path(path).mkdir(mode=mode, parents=True, exist_ok=True)
            Path(path).chmod(mode)

        with mock.patch.object(update, "PACKAGE_ROLLBACK_ROOT", rollback), \
             mock.patch.object(update, "root_directory", side_effect=permissive_root_directory), \
             mock.patch.object(update.os, "chown"):
            manifest = update._retain_installed_package_pair(candidate)

        self.assertEqual(manifest, rollback / "manifest.json")
        retained = update.update_packages.load_installed_rollback(
            ("omarchy", "omarchy-settings"), roots=(rollback,)
        )
        self.assertEqual({package.version for package in retained}, {"2.0-1"})
        self.assertEqual(
            json.loads((rollback / "manifest.json").read_text())["source_revision"],
            "b" * 40,
        )

    def test_previous_pair_snapshot_survives_publication_of_new_active_pair(self):
        rollback = self.base / "usr/share/omarchy-pi/rollback"
        rollback.parent.mkdir(parents=True)
        previous_root = self.base / "state/updates/job/previous"
        previous_root.parent.mkdir(parents=True)
        packages = []

        def candidate(version, revision, signed=False):
            result = []
            for name in ("omarchy", "omarchy-settings"):
                archive = self.base / f"{name}-{version}-aarch64.pkg.tar.zst"
                self._native_archive(archive, name, version, revision)
                signature = None
                if signed:
                    signature = archive.with_name(archive.name + ".sig")
                    signature.write_bytes(b"detached-signature\n")
                result.append(update.update_packages.CandidatePackage(
                    name=name,
                    version=version,
                    architecture="aarch64",
                    archive=archive,
                    sha256=update.update_packages._digest(archive),
                    signature="required" if signed else "optional",
                    signature_file=signature,
                ))
            return SimpleNamespace(packages=tuple(result), source_revision=revision)

        old = candidate("1.0-1", "a" * 40, signed=True)
        new = candidate("2.0-1", "b" * 40)

        def permissive_root_directory(path, mode=0o755, gid=0):
            Path(path).mkdir(mode=mode, parents=True, exist_ok=True)
            Path(path).chmod(mode)

        with mock.patch.object(update, "PACKAGE_ROLLBACK_ROOT", rollback), \
             mock.patch.object(update, "root_directory", side_effect=permissive_root_directory), \
             mock.patch.object(update.os, "chown"):
            update._retain_installed_package_pair(old)
            installed = update.update_packages.load_installed_rollback(
                ("omarchy", "omarchy-settings"), roots=(rollback,)
            )
            snapshot = update._snapshot_previous_package_pair(installed, previous_root, 1000)
            rollback_result = [
                {"archive": str(package.archive), "sha256": package.sha256}
                for package in snapshot
            ]
            update._retain_installed_package_pair(new)

        retained = update.update_packages.load_installed_rollback(
            ("omarchy", "omarchy-settings"), roots=(previous_root,)
        )
        active = update.update_packages.load_installed_rollback(
            ("omarchy", "omarchy-settings"), roots=(rollback,)
        )
        self.assertEqual({package.version for package in active}, {"2.0-1"})
        self.assertEqual({package.version for package in retained}, {"1.0-1"})
        self.assertTrue((previous_root / "manifest.json").is_file())
        self.assertTrue(all(Path(record["archive"]).parent == previous_root for record in rollback_result))
        for package in retained:
            self.assertEqual(update.update_packages._digest(package.archive), package.sha256)
            self.assertTrue(package.signature_file is not None and package.signature_file.is_file())

    def test_rollback_publication_keeps_old_pair_if_manifest_replace_is_interrupted(self):
        rollback = self.base / "usr/share/omarchy-pi/rollback"
        rollback.parent.mkdir(parents=True)

        def candidate(version, revision):
            packages = []
            for name in ("omarchy", "omarchy-settings"):
                archive = self.base / f"{name}-{version}-aarch64.pkg.tar.zst"
                self._native_archive(archive, name, version, revision)
                packages.append(update.update_packages.CandidatePackage(
                    name=name,
                    version=version,
                    architecture="aarch64",
                    archive=archive,
                    sha256=update.update_packages._digest(archive),
                    signature="optional",
                ))
            return SimpleNamespace(packages=tuple(packages), source_revision=revision)

        old = candidate("1.0-1", "a" * 40)
        new = candidate("2.0-1", "b" * 40)
        with mock.patch.object(update, "PACKAGE_ROLLBACK_ROOT", rollback), \
             mock.patch.object(update, "root_directory", side_effect=lambda path, mode=0o755, gid=0: Path(path).mkdir(mode=mode, parents=True, exist_ok=True)), \
             mock.patch.object(update.os, "chown"):
            update._retain_installed_package_pair(old)

            original_replace = update.os.replace

            def interrupt_manifest(source, destination):
                if Path(destination).name == "manifest.json":
                    raise OSError("simulated publication interruption")
                return original_replace(source, destination)

            with mock.patch.object(update.os, "replace", side_effect=interrupt_manifest):
                with self.assertRaises(OSError):
                    update._retain_installed_package_pair(new)
            retained = update.update_packages.load_installed_rollback(
                ("omarchy", "omarchy-settings"), roots=(rollback,)
            )
            self.assertEqual({package.version for package in retained}, {"1.0-1"})

    def test_worker_start_failure_persists_failed_result(self):
        commands = []
        root_calls = []

        def failing_command(argv, **kwargs):
            commands.append(list(argv))
            if argv[0] == "/usr/bin/systemd-run":
                raise RuntimeError("systemd-run unavailable")
            if argv[:3] == ["/usr/bin/systemctl", "show", update.UNIT]:
                return SimpleNamespace(stdout="inactive\n", returncode=0)
            return SimpleNamespace(stdout="", returncode=0)

        with mock.patch.dict(update.os.environ, {"SUDO_UID": str(self.account.pw_uid)}, clear=False), self._launch_patches(commands, root_calls), mock.patch.object(update, "command", side_effect=failing_command):
            with self.assertRaisesRegex(RuntimeError, "systemd-run unavailable"):
                update.launch()

        jobs = [path for path in (self.base / "state/updates").iterdir() if path.is_dir()]
        self.assertEqual(len(jobs), 1)
        result = json.loads((jobs[0] / "result.json").read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phase"], "starting")
        reset = ["/usr/bin/systemctl", "reset-failed", update.UNIT]
        self.assertLess(commands.index(reset), next(index for index, command in enumerate(commands) if command[0] == "/usr/bin/systemd-run"))

    def test_active_release_requires_matching_marker(self):
        (self.release / ".omarchy-pi-source-commit").write_text("b" * 40 + "\n", encoding="utf-8")
        with self.assertRaisesRegex(update.UpdateError, "does not match"):
            update.active_release(self.account)

    def test_previous_migration_snapshot_is_group_readable_under_restrictive_umask(self):
        migrations = self.release / "migrations"
        migrations.mkdir(mode=0o700)
        migration = migrations / "old-migration.sh"
        migration.write_text("#!/bin/bash\n", encoding="utf-8")
        migration.chmod(0o600)
        destination = self.base / "job" / "old-runtime"
        old_umask = os.umask(0o077)
        try:
            with mock.patch.object(update.os, "chown") as chown:
                snapshot = update._copy_previous_migrations(self.release, destination)
                update._make_candidate_snapshot_readable(snapshot, self.account.pw_gid)
        finally:
            os.umask(old_umask)

        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode) & 0o050, 0o050)
        self.assertEqual(stat.S_IMODE((snapshot / "migrations").stat().st_mode) & 0o050, 0o050)
        self.assertEqual(stat.S_IMODE((snapshot / "migrations/old-migration.sh").stat().st_mode) & 0o040, 0o040)
        chown.assert_any_call(snapshot, 0, self.account.pw_gid, follow_symlinks=False)
        chown.assert_any_call(snapshot / "migrations", 0, self.account.pw_gid, follow_symlinks=False)
        chown.assert_any_call(snapshot / "migrations/old-migration.sh", 0, self.account.pw_gid, follow_symlinks=False)

    def test_worker_rejects_account_identity_change_before_work(self):
        state_root = self.base / "state/updates"
        job = state_root / "job"
        job.mkdir(parents=True)
        (job / "request.json").write_text(json.dumps({"username": "another-user", "uid": self.account.pw_uid, "gid": self.account.pw_gid, "home": self.account.pw_dir, "old_release": str(self.release)}) + "\n", encoding="utf-8")
        (job / "runtime").mkdir()
        original_stat = Path.stat

        def fake_stat(path, *args, **kwargs):
            if path == job:
                return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o700)
            return original_stat(path, *args, **kwargs)

        with mock.patch.object(update, "STATE_ROOT", state_root), mock.patch.object(update, "require_pi"), mock.patch.object(update.os, "geteuid", return_value=0), mock.patch.object(update.pwd, "getpwuid", return_value=self.account), mock.patch.object(update.Path, "stat", fake_stat):
            with self.assertRaisesRegex(update.UpdateError, "account changed"):
                update.worker(job)

    def test_report_detects_interrupted_stale_worker(self):
        state_root = self.base / "state/updates"
        job = state_root / "job"
        job.mkdir(parents=True)
        (job / "result.json").write_text(json.dumps({"status": "running", "phase": "update-packages", "started_at": 100.0}) + "\n", encoding="utf-8")
        (job / "update.log").write_text("worker started\n", encoding="utf-8")
        with mock.patch.object(update.subprocess, "run", return_value=SimpleNamespace(returncode=3)), mock.patch.object(update.time, "time", return_value=200.0), mock.patch.object(update.time, "sleep"):
            output = io.StringIO()
            with redirect_stdout(output):
                status = update.report(job, True)
        self.assertEqual(status, 1)
        self.assertIn("Update failed; phase: update-packages", output.getvalue())

    def test_worker_stops_before_activation_when_package_transaction_fails(self):
        state_root = self.base / "state/updates"
        job = state_root / "job"
        job.mkdir(parents=True)
        (job / "runtime").mkdir()
        (job / "request.json").write_text(json.dumps({"username": self.account.pw_name, "uid": self.account.pw_uid, "gid": self.account.pw_gid, "home": self.account.pw_dir, "old_release": str(self.release)}) + "\n", encoding="utf-8")
        calls = []
        candidate = "c" * 40
        checkout = self.base / "checkout"

        def fake_command(argv, **kwargs):
            calls.append(list(argv))
            if "/usr/bin/pacman" in argv and "-Su" in argv:
                raise RuntimeError("package transaction failed")
            if "/usr/bin/pacman" in argv and "-Sup" in argv:
                return SimpleNamespace(stdout="linux-rpi 1.0\n", returncode=0)
            return SimpleNamespace(stdout="", returncode=0)

        def fake_user_command(account, argv, **kwargs):
            calls.append(list(argv))
            if any("update-source.py" in str(item) for item in argv):
                return SimpleNamespace(stdout=json.dumps({"revision": candidate, "checkout": str(checkout)}))
            return SimpleNamespace(stdout="")

        original_stat = Path.stat

        def fake_stat(path, *args, **kwargs):
            if path == job:
                return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o700)
            return original_stat(path, *args, **kwargs)

        def fake_write_json(path, value, gid):
            Path(path).write_text(json.dumps(value) + "\n", encoding="utf-8")

        with mock.patch.object(update, "STATE_ROOT", state_root), mock.patch.object(update, "require_pi"), mock.patch.object(update.os, "geteuid", return_value=0), mock.patch.object(update.os, "chown") as chown, mock.patch.object(update.pwd, "getpwuid", return_value=self.account), mock.patch.object(update, "active_release", return_value=self.release), mock.patch.object(update, "command", side_effect=fake_command), mock.patch.object(update, "user_command", side_effect=fake_user_command), mock.patch.object(update, "write_json", side_effect=fake_write_json), mock.patch.object(update.Path, "stat", fake_stat):
            status = update.worker(job)

        self.assertEqual(status, 1)
        result = json.loads((job / "result.json").read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phase"], "update-packages")
        package_plan = job / "package-plan.txt"
        self.assertEqual(stat.S_IMODE(package_plan.stat().st_mode), 0o640)
        chown.assert_any_call(package_plan, 0, self.account.pw_gid)
        self.assertFalse(any("activate" in call for call in calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
