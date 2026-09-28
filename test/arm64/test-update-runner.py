#!/usr/bin/python3
"""Focused contract tests for the durable Pi update runner."""

from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import stat
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

        with mock.patch.object(update, "STATE_ROOT", state_root), mock.patch.object(update, "require_pi"), mock.patch.object(update.os, "geteuid", return_value=0), mock.patch.object(update.pwd, "getpwuid", return_value=self.account), mock.patch.object(update, "active_release", return_value=self.release), mock.patch.object(update, "command", side_effect=fake_command), mock.patch.object(update, "user_command", side_effect=fake_user_command), mock.patch.object(update, "write_json", side_effect=fake_write_json), mock.patch.object(update.Path, "stat", fake_stat):
            status = update.worker(job)

        self.assertEqual(status, 1)
        result = json.loads((job / "result.json").read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phase"], "update-packages")
        self.assertFalse(any("activate" in call for call in calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
