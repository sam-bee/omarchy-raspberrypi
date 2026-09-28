#!/usr/bin/python3
"""Unprivileged contract tests for the installer controller and worker."""

from __future__ import annotations

import contextlib
import importlib.util
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("installer_job_under_test", HERE / "installer_job.py")
assert SPEC and SPEC.loader
job = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = job
SPEC.loader.exec_module(job)


SECRET = "correct horse battery staple"
WIFI_SECRET = "wifi-secret"


def settings(*, encryption: str = "plain") -> dict[str, object]:
    return {
        "username": "pi-user",
        "hostname": "pi-target",
        "password": SECRET,
        "timezone": "Europe/London",
        "locale": "en_GB.UTF-8",
        "keymap": "us",
        "wifi": {"country": "GB", "ssid": "home", "password": WIFI_SECRET},
        "ssh_enabled": True,
        "ssh_authorized_key": "ssh-ed25519 " + "A" * 44,
        "rdp_mode": "loopback",
        "rdp_password": "rdp-secret",
        "encryption": encryption,
        "recovery_passphrase": "recovery-secret" if encryption != "plain" else None,
    }


class FakeDisk(types.ModuleType):
    def __init__(self, root: Path) -> None:
        super().__init__("disk_install")
        self.root = root
        self.prepare_calls: list[tuple[object, ...]] = []

    def discover_disks(self) -> list[dict[str, object]]:
        return [
            {"path": "/dev/nvme0n1", "identity": "target-identity", "size": 128 * 1024**3, "eligible": True, "reasons": []},
            {"path": "/dev/sdb", "identity": "key-identity", "size": 256 * 1024**2, "eligible": True, "reasons": []},
        ]

    def select_disk(self, path: str) -> dict[str, object]:
        if path == "/dev/nvme0n1":
            return {"path": path, "identity": "target-identity", "size": 128 * 1024**3, "eligible": True, "reasons": []}
        if path == "/dev/sdb":
            return {"path": path, "identity": "key-identity", "size": 256 * 1024**2, "eligible": True, "reasons": []}
        raise RuntimeError("unknown test disk")

    def confirm_token(self, identity: dict[str, object]) -> str:
        return "CONFIRM " + str(identity["path"])

    def validate_pair(self, target, key):
        target_path = target.get("path") if isinstance(target, dict) else target
        key_path = key.get("path") if isinstance(key, dict) else key
        if target_path != "/dev/nvme0n1" or key_path not in {None, "/dev/sdb"}:
            raise RuntimeError("invalid pair")
        return target, key

    @contextlib.contextmanager
    def prepare_target(self, target: str, mode: str, passphrase: str | None, key: str | None):
        self.prepare_calls.append((target, mode, passphrase, key))
        root = self.root / "target-root"
        root.mkdir()
        yield {
            "root": root,
            "boot": self.root / "target-boot",
            "root_uuid": "root-uuid",
            "boot_uuid": "boot-uuid",
            "luks_uuid": None,
            "key_uuid": None,
            "key_path": None,
        }


class FakeTarget(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("installed_target")
        self.received: list[dict[str, object]] = []
        self.received_provenance: list[dict[str, object] | None] = []
        self.fail = False

    def validate_settings(self, value: dict[str, object]) -> dict[str, object]:
        return dict(value)

    def provision_target(
        self,
        root: Path,
        payload: Path,
        value: dict[str, object],
        storage: dict[str, object],
        progress,
        *,
        provenance: dict[str, object] | None = None,
    ):
        self.received.append(value)
        self.received_provenance.append(provenance)
        progress("target settings accepted")
        if self.fail:
            raise RuntimeError("provision failed with password=" + SECRET)
        return {"root": str(root), "secret": SECRET, "result": "ok"}


class FakePayload(types.ModuleType):
    def __init__(self, root: Path) -> None:
        super().__init__("desktop_payload")
        self.root = root
        self.copy_calls: list[tuple[Path, Path, Path]] = []

    def payload_metadata(self) -> dict[str, object]:
        return {"source_revision": "abc123", "required_target_bytes": 42, "unpacked_bytes": 84, "sha256": "f" * 64}

    def prepare_payload(self) -> Path:
        self.root.mkdir(exist_ok=True)
        return self.root

    def reset_incomplete_payload(self) -> None:
        return None

    def copy_payload(self, payload: Path, target_root: Path, target_boot: Path) -> None:
        self.copy_calls.append((payload, target_root, target_boot))


class InstallerJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        job.STATE_ROOT = root / "state"
        job.RUNTIME_ROOT = root / "run"
        job._ensure_directories()
        self.disk = FakeDisk(root)
        self.target = FakeTarget()
        self.payload = FakePayload(root / "payload")
        self.modules = {"disk_install": self.disk, "installed_target": self.target, "desktop_payload": self.payload}
        self.original_provenance_path = job.INSTALLER_PROVENANCE_PATH
        self.provenance_path = root / "installer-provenance.json"
        self.provenance_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_revision": "a" * 40,
                    "runtime_sha256": "b" * 64,
                    "files": {"usr/local/libexec/omarchy-pi/installer_job.py": "c" * 64},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.provenance_path.chmod(0o600)
        job.INSTALLER_PROVENANCE_PATH = self.provenance_path
        self.addCleanup(self.restore_provenance_path)
        self.previous_modules: dict[str, object] = {}
        for name, module in self.modules.items():
            self.previous_modules[name] = sys.modules.get(name)
            sys.modules[name] = module
        self.addCleanup(self.restore_modules)
        self.started = 0
        self.original_start = job._start_service
        job._start_service = self.start_service
        self.addCleanup(self.restore_start)
        self.original_network_preflight = job._network_preflight
        job._network_preflight = lambda: None
        self.addCleanup(self.restore_network_preflight)

    def restore_modules(self) -> None:
        for name, previous in self.previous_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def restore_provenance_path(self) -> None:
        job.INSTALLER_PROVENANCE_PATH = self.original_provenance_path

    def restore_start(self) -> None:
        job._start_service = self.original_start

    def restore_network_preflight(self) -> None:
        job._network_preflight = self.original_network_preflight

    def start_service(self) -> None:
        self.started += 1

    def request(self, *, action: str = "submit", encryption: str = "plain") -> dict[str, object]:
        target = "/dev/nvme0n1"
        key = "/dev/sdb" if encryption == "key" else None
        return {
            "action": action,
            "settings": settings(encryption=encryption),
            "target": target,
            "key": key,
            "target_confirmation": "CONFIRM " + target,
            "key_confirmation": "CONFIRM " + key if key else None,
            "consent_internet": True,
            "restart_confirmation": job.RESTART_CONFIRMATION,
        }

    def submit(self, *, encryption: str = "plain") -> dict[str, object]:
        return job._handle_request(self.request(encryption=encryption))

    def fake_recovery(self, *, fail=False):
        calls = []

        def handle(request):
            calls.append(dict(request))
            if fail and request["action"] == "recovery-repair":
                raise RuntimeError("repair failed with " + SECRET)
            return {"target": {"path": "/dev/mmcblk0", "serial": "test-card"}, "repaired": request["action"] == "recovery-repair"}

        recovery = types.SimpleNamespace(REPAIR_CONFIRMATION="REPAIR BOOT ONLY", RecoveryError=RuntimeError, handle_request=handle)
        self.previous_modules["recovery"] = sys.modules.get("recovery")
        sys.modules["recovery"] = recovery
        return calls

    def recovery_request(self):
        return {"action": "recovery-repair", "target": "/dev/mmcblk0", "target_confirmation": "RECOVER test-card", "repair_confirmation": "REPAIR BOOT ONLY", "passphrase": SECRET}

    def test_recovery_queues_private_credentials_and_reuses_worker_without_formatting(self):
        calls = self.fake_recovery()
        response = job._handle_request(self.recovery_request())
        self.assertEqual(self.started, 1)
        self.assertEqual(response["state"]["kind"], "recovery")
        self.assertNotIn(SECRET, json.dumps(response))
        self.assertNotIn(SECRET, job._state_path().read_text())
        self.assertEqual(calls[0]["action"], "recovery-plan")
        self.assertEqual(job._run_worker(), 0)
        self.assertEqual(calls[-1]["action"], "recovery-repair")
        self.assertEqual(self.disk.prepare_calls, [])
        self.assertEqual(self.target.received, [])
        self.assertEqual(job._load_state()["phase"], "recovery-complete")
        self.assertFalse(job._request_path().exists())

    def test_recovery_requires_confirmation_and_excludes_other_jobs(self):
        self.fake_recovery()
        with self.assertRaises(job.InstallerError):
            job._handle_request({**self.recovery_request(), "repair_confirmation": "yes"})
        self.submit()
        with mock.patch.object(job, "_service_is_active", return_value=True):
            with self.assertRaises(job.InstallerError):
                job._handle_request(self.recovery_request())
            with self.assertRaises(job.InstallerError):
                job._handle_request({"action": "recovery-inspect"})

    def test_recovery_failure_redacts_secret_and_deletes_request(self):
        self.fake_recovery(fail=True)
        job._handle_request(self.recovery_request())
        self.assertEqual(job._run_worker(), 1)
        state = job._load_state()
        self.assertEqual(state["status"], "failed")
        self.assertNotIn(SECRET, job._state_path().read_text())
        self.assertNotIn(SECRET, job._log_path(state["job_id"]).read_text())
        self.assertFalse(job._request_path().exists())

    def test_recovery_refuses_changed_runtime_before_target_access(self):
        calls = self.fake_recovery()
        job._handle_request(self.recovery_request())
        record = json.loads(self.provenance_path.read_text())
        record["runtime_sha256"] = "d" * 64
        self.provenance_path.write_text(json.dumps(record))
        self.assertEqual(job._run_worker(), 1)
        self.assertEqual([call["action"] for call in calls], ["recovery-plan"])
        self.assertFalse(job._request_path().exists())

    def test_worker_discards_mismatched_request_without_target_access(self):
        calls = self.fake_recovery()
        job_id = "queued-job-12345678"
        job._save_state({"status": "queued", "phase": "recovery-queued", "job_id": job_id})
        job._write_request(
            {
                "kind": "recovery",
                "job_id": "other-job-12345678",
                "recovery": self.recovery_request(),
            }
        )

        self.assertEqual(job._run_worker(), 1)
        state = job._load_state()
        self.assertEqual(state["status"], "interrupted")
        self.assertFalse(job._request_path().exists())
        self.assertEqual(calls, [])
        self.assertNotIn(SECRET, json.dumps(state))
        self.assertNotIn(SECRET, job._log_path(job_id).read_text())

    def test_worker_discards_malformed_recovery_without_target_access(self):
        calls = self.fake_recovery()
        job_id = "malformed-job-12345678"
        job._save_state({"status": "queued", "phase": "recovery-queued", "job_id": job_id})
        job._write_request(
            {
                "kind": "recovery",
                "job_id": job_id,
                "settings": {"password": SECRET},
                "recovery": "not-a-mapping",
            }
        )

        self.assertEqual(job._run_worker(), 1)
        state = job._load_state()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"], "queued recovery request is invalid")
        self.assertFalse(job._request_path().exists())
        self.assertEqual(calls, [])
        self.assertNotIn(SECRET, json.dumps(state))
        self.assertNotIn(SECRET, job._log_path(job_id).read_text())

    def test_worker_discards_unreadable_request_without_target_access(self):
        calls = self.fake_recovery()
        job_id = "unreadable-job-12345678"
        sentinel = "malformed-request-sentinel"
        job._save_state({"status": "queued", "phase": "recovery-queued", "job_id": job_id})
        job._request_path().write_text(
            f'{{"job_id":"{job_id}","passphrase":"{sentinel}",',
            encoding="utf-8",
        )

        self.assertEqual(job._run_worker(), 1)
        state = job._load_state()
        self.assertEqual(state["status"], "interrupted")
        self.assertEqual(state["error"], "the queued request is unreadable; explicit restart is required")
        self.assertFalse(job._request_path().exists())
        self.assertEqual(calls, [])
        self.assertNotIn(sentinel, json.dumps(state))
        self.assertNotIn(sentinel, job._log_path(job_id).read_text())

    def test_plan_is_secret_free_and_requires_explicit_confirmation(self) -> None:
        plan = job._handle_request({**self.request(), "action": "plan"})
        encoded = json.dumps(plan)
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn(WIFI_SECRET, encoded)
        self.assertIn("CONFIRM /dev/nvme0n1", encoded)
        self.assertEqual(plan["installer"]["source_revision"], "a" * 40)
        self.assertEqual(plan["installer"]["runtime_sha256"], "b" * 64)
        self.assertNotIn("files", plan["installer"])
        with self.assertRaises(job.InstallerError):
            job._handle_request({**self.request(), "action": "submit", "target_confirmation": "wrong"})
        self.assertEqual(self.started, 0)

    def test_submit_persists_only_nonsecret_state_and_starts_service(self) -> None:
        result = self.submit()
        self.assertEqual(self.started, 1)
        self.assertEqual(result["state"]["status"], "queued")
        self.assertEqual(result["state"]["installer"]["runtime_sha256"], "b" * 64)
        self.assertNotIn("files", result["state"]["installer"])
        state_text = (job.STATE_ROOT / "state.json").read_text()
        self.assertNotIn(SECRET, state_text)
        self.assertNotIn(WIFI_SECRET, state_text)
        private_request = json.loads((job.RUNTIME_ROOT / "request.json").read_text())
        self.assertEqual(private_request["settings"]["password"], SECRET)
        self.assertEqual(job._load_state()["phase"], "queued")

    def test_worker_runs_in_background_and_removes_transient_secrets(self) -> None:
        self.submit()
        original_sigterm = signal.getsignal(signal.SIGTERM)
        self.assertEqual(job._run_worker(), 0)
        self.assertIs(signal.getsignal(signal.SIGTERM), original_sigterm)
        state = job._load_state()
        self.assertEqual(state["status"], "complete")
        self.assertFalse((job.RUNTIME_ROOT / "request.json").exists())
        log = (job.STATE_ROOT / "jobs" / f"{state['job_id']}.log").read_text()
        self.assertNotIn(SECRET, log)
        self.assertNotIn(WIFI_SECRET, log)
        self.assertEqual(self.disk.prepare_calls[0][1], "plain")
        self.assertEqual(len(self.payload.copy_calls), 1)
        self.assertEqual(self.target.received[0]["password"], SECRET)
        self.assertEqual(
            self.target.received_provenance[0],
            {
                "installer": {
                    "source_revision": "a" * 40,
                    "runtime_sha256": "b" * 64,
                },
                "desktop_bundle_sha256": "f" * 64,
            },
        )
        self.assertEqual(state["message"], "target settings accepted")
        self.assertNotIn(SECRET, json.dumps(job._safe_state(state)))

    def test_bad_installer_provenance_is_rejected_before_submit(self) -> None:
        self.provenance_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_revision": "not-a-revision",
                    "runtime_sha256": "b" * 64,
                    "files": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(job.InstallerError, "source revision"):
            job._handle_request({**self.request(), "action": "plan"})

    def test_changed_installer_provenance_fails_before_target_preparation(self) -> None:
        self.submit()
        changed = {"source_revision": "d" * 40, "runtime_sha256": "b" * 64}
        with mock.patch.object(job, "_installer_provenance", return_value=changed):
            self.assertEqual(job._run_worker(), 1)
        state = job._load_state()
        self.assertEqual(state["status"], "failed")
        self.assertIn("changed since the job was queued", state["error"])
        self.assertEqual(self.disk.prepare_calls, [])

    def test_absolute_installer_provenance_file_path_is_rejected(self) -> None:
        self.provenance_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_revision": "a" * 40,
                    "runtime_sha256": "b" * 64,
                    "files": {"/usr/local/libexec/omarchy-pi/installer_job.py": "c" * 64},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(job.InstallerError, "file path"):
            job._handle_request({**self.request(), "action": "plan"})

    def test_redirected_installer_provenance_is_rejected(self) -> None:
        redirected = self.provenance_path.with_name("provenance-link")
        redirected.symlink_to(self.provenance_path)
        original = job.INSTALLER_PROVENANCE_PATH
        job.INSTALLER_PROVENANCE_PATH = redirected
        try:
            with self.assertRaisesRegex(job.InstallerError, "redirected"):
                job._installer_provenance()
        finally:
            job.INSTALLER_PROVENANCE_PATH = original

    def test_worker_error_redacts_secret_and_marks_failed(self) -> None:
        self.submit()
        self.target.fail = True
        self.assertEqual(job._run_worker(), 1)
        state = job._load_state()
        self.assertEqual(state["status"], "failed")
        self.assertNotIn(SECRET, json.dumps(state))
        self.assertNotIn(SECRET, (job.STATE_ROOT / "jobs" / f"{state['job_id']}.log").read_text())

    def test_stale_running_job_becomes_interrupted_without_resume(self) -> None:
        self.submit()
        state = job._load_state()
        state["status"] = "running"
        state["phase"] = "desktop-provisioning"
        job._save_state(state)
        status = job._handle_request({"action": "status"})["state"]
        self.assertEqual(status["status"], "interrupted")
        self.assertEqual(job._run_worker(), 0)
        self.assertEqual(job._load_state()["status"], "interrupted")

    def test_restart_requires_phrase_and_creates_new_job(self) -> None:
        self.submit()
        state = job._load_state()
        state["status"] = "running"
        job._save_state(state)
        job._handle_request({"action": "status"})
        with self.assertRaises(job.InstallerError):
            job._handle_request({**self.request(action="restart"), "restart_confirmation": "yes"})
        result = job._handle_request(self.request(action="restart"))
        self.assertNotEqual(result["job_id"], state["job_id"])
        self.assertEqual(job._load_state()["previous_job_id"], state["job_id"])

    def test_worker_lock_is_exclusive(self) -> None:
        with job._file_lock(job.RUNTIME_ROOT / job.WORKER_LOCK_NAME):
            self.assertTrue(job._worker_is_running())

    def test_sigterm_unwinds_storage_context_and_needs_explicit_status(self) -> None:
        root = Path(self.temporary.name) / "sigterm-worker"
        root.mkdir()
        script = textwrap.dedent(
            f"""
            import contextlib
            import importlib.util
            from pathlib import Path
            import sys
            import time
            import types

            base = Path(sys.argv[1])
            source = Path({str(HERE / 'installer_job.py')!r})
            spec = importlib.util.spec_from_file_location("sigterm_worker_job", source)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            module.STATE_ROOT = base / "state"
            module.RUNTIME_ROOT = base / "run"
            module.SERVICE_NAME = "omarchy-step4-sigterm-test.service"
            provenance = base / "installer-provenance.json"
            provenance.write_text('{{"schema_version":1,"source_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","runtime_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","files":{{"installer_job.py":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}}}}')
            provenance.chmod(0o600)
            module.INSTALLER_PROVENANCE_PATH = provenance
            module._ensure_directories()
            target_path = "/dev/omarchy-step4-sigterm-target"
            identity = {{"path": target_path, "size": 4 * 1024 * 1024 * 1024}}

            disk = types.ModuleType("disk_install")
            disk.BOOT_SIZE_MIB = 1
            disk.select_disk = lambda path: identity
            disk.confirm_token = lambda value: "CONFIRM " + value["path"]
            disk.validate_pair = lambda target, key: (target, key)

            @contextlib.contextmanager
            def prepare_target(target, mode, passphrase, key):
                mounted = base / "mounted"
                (mounted / "root").mkdir(parents=True)
                (mounted / "boot").mkdir()
                (base / "entered").write_text("entered")
                try:
                    yield {{"root": mounted / "root", "boot": mounted / "boot", "root_uuid": "root", "boot_uuid": "boot", "luks_uuid": None, "key_uuid": None, "key_path": None}}
                finally:
                    (base / "cleanup").write_text("cleanup")

            disk.prepare_target = prepare_target
            payload = types.ModuleType("desktop_payload")
            payload_root = base / "payload"
            payload.payload_metadata = lambda: {{"source_revision": "a" * 40, "required_target_bytes": 1, "unpacked_bytes": 1, "sha256": "b" * 64}}
            def prepare_payload():
                payload_root.mkdir()
                return payload_root

            payload.prepare_payload = prepare_payload
            payload.copy_payload = lambda source, target_root, target_boot: None
            target = types.ModuleType("installed_target")
            target.validate_settings = lambda value: dict(value)
            target.validate_target_options = lambda root, value: True

            def provision_target(root, payload, settings, storage, progress, *, provenance=None):
                (base / "provisioning").write_text("entered")
                while True:
                    time.sleep(1)

            target.provision_target = provision_target
            sys.modules.update({{"disk_install": disk, "desktop_payload": payload, "installed_target": target}})
            settings = {{"username": "pi-user", "hostname": "sigterm-test", "password": "sigterm-secret", "timezone": "Europe/London", "locale": "en_GB.UTF-8", "keymap": "us", "wifi": None, "ssh_enabled": False, "ssh_authorized_key": None, "rdp_mode": "disabled", "rdp_password": None, "encryption": "plain", "recovery_passphrase": None}}
            job_id = "sigterm-job-12345678"
            module._write_request({{"job_id": job_id, "settings": settings, "target": target_path, "key": None, "target_identity": identity, "key_identity": None, "target_token": "CONFIRM " + target_path, "key_token": None, "required_target_bytes": 1, "consent_internet": True}})
            module._save_state({{"status": "queued", "phase": "queued", "job_id": job_id, "target_path": target_path, "installer": {{"source_revision": "a" * 40, "runtime_sha256": "b" * 64}}}})
            module._network_preflight = lambda: None
            try:
                module._run_worker()
            except KeyboardInterrupt:
                pass
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", script, str(root)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            entered = root / "provisioning"
            deadline = time.monotonic() + 10
            while not entered.exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            if not entered.exists():
                stdout, stderr = process.communicate(timeout=2)
                self.fail(f"SIGTERM worker did not enter target context: {stdout} {stderr}")
            state_before = json.loads((root / "state" / "state.json").read_text())
            self.assertEqual(state_before["status"], "running")
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        self.assertTrue((root / "cleanup").is_file(), "SIGTERM skipped storage context cleanup")
        self.assertFalse((root / "run" / "request.json").exists())
        original_state = job.STATE_ROOT
        original_runtime = job.RUNTIME_ROOT
        job.STATE_ROOT = root / "state"
        job.RUNTIME_ROOT = root / "run"
        try:
            with mock.patch.object(job, "_service_is_active", return_value=False):
                status = job._handle_request({"action": "status"})["state"]
            self.assertEqual(status["status"], "interrupted")
            self.assertEqual(job._run_worker(), 0)
            self.assertEqual((root / "cleanup").read_text(), "cleanup")
        finally:
            job.STATE_ROOT = original_state
            job.RUNTIME_ROOT = original_runtime

    def test_network_preflight_uses_only_fixed_url_and_is_bounded(self) -> None:
        calls = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(request, *, timeout):
            calls.append((request.full_url, request.method, timeout))
            return Response()

        with mock.patch.object(job.urllib.request, "urlopen", opener):
            self.original_network_preflight()
        self.assertEqual(calls, [(job.NETWORK_PREFLIGHT_URLS[0], "HEAD", job.NETWORK_PREFLIGHT_TIMEOUT)])
        self.assertNotIn("password", job.NETWORK_PREFLIGHT_URLS[0])

    def test_isolated_python_loads_whitelisted_sibling_module(self) -> None:
        script = f"""
import importlib.util
from pathlib import Path
import sys
source = Path({str(HERE / 'installer_job.py')!r})
spec = importlib.util.spec_from_file_location('isolated_installer_job', source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module._MODULE_SEARCH_DIRS = [source.parent]
assert module._module('installed_target').validate_settings
print('ok')
"""
        result = subprocess.run(
            ["/usr/bin/python3", "-I", "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_frontend_watches_a_new_submission_to_completion(self) -> None:
        actions: list[str] = []
        status_calls = 0
        inputs = iter([
            "/dev/nvme0n1",
            "pi-user",
            "pi-target",
            "",
            "",
            "",
            "no",
            "no",
            "disabled",
            "plain",
            "",
            "CONFIRM /dev/nvme0n1",
            "YES",
            "",
        ])
        prompts: list[str] = []

        def call(request):
            nonlocal status_calls
            actions.append(request["action"])
            if request["action"] == "status":
                status_calls += 1
                if status_calls == 1:
                    return {"state": {"status": "idle"}}
                if status_calls == 2:
                    return {
                        "state": {
                            "status": "complete",
                            "phase": "complete",
                            "message": "target configuration finished",
                            "summary": {"hostname": "pi-target"},
                        }
                    }
            if request["action"] == "discover":
                return {"disks": []}
            if request["action"] == "plan":
                return {"settings": {}, "payload": {}, "target": {"token": "CONFIRM /dev/nvme0n1"}, "key": None}
            if request["action"] == "submit":
                return {"job_id": "native-test-job", "state": {"status": "queued", "phase": "queued", "job_id": "native-test-job"}}
            raise AssertionError(request)

        output: list[str] = []
        result = job.frontend_main(
            input_fn=lambda prompt: (prompts.append(prompt) or next(inputs)),
            secret_fn=lambda prompt: "target-password",
            output=output.append,
            call=call,
            sleep_fn=lambda seconds: None,
        )
        self.assertEqual(result, 0)
        self.assertIn("Installation complete", "\n".join(output))
        self.assertIn("remove the installer USB", "\n".join(output))
        self.assertIn("target configuration finished", "\n".join(output))
        self.assertIn("Press Enter to close", "\n".join(prompts))
        self.assertEqual(actions, ["status", "discover", "plan", "submit", "status"])

    def test_state_and_runtime_roots_reject_symlink_redirection(self) -> None:
        real = Path(self.temporary.name) / "real-state"
        real.mkdir()
        redirected = Path(self.temporary.name) / "redirected-state"
        redirected.symlink_to(real, target_is_directory=True)
        original = job.STATE_ROOT
        job.STATE_ROOT = redirected
        try:
            with self.assertRaises(job.InstallerError):
                job._ensure_directories()
        finally:
            job.STATE_ROOT = original

    def test_state_root_rejects_other_writable_final_directory(self) -> None:
        state = Path(self.temporary.name) / "writable-state"
        state.mkdir(mode=0o700)
        state.chmod(0o777)
        original = job.STATE_ROOT
        job.STATE_ROOT = state
        try:
            with self.assertRaises(job.InstallerError):
                job._ensure_directories()
        finally:
            job.STATE_ROOT = original

    def test_unit_and_wrappers_have_fixed_nonrestarting_contract(self) -> None:
        unit = (HERE / "omarchy-pi-install.service").read_text()
        self.assertIn("ExecStart=/usr/bin/python3 -I /usr/local/libexec/omarchy-pi/installer_job.py --worker", unit)
        self.assertIn("Restart=no", unit)
        self.assertNotIn("\n[Install]\n", unit)
        control = (HERE / "installer-control").read_text()
        self.assertIn("#!/usr/bin/python3 -I", control)
        self.assertIn('os.environ.clear()', control)
        self.assertIn('"--control"', control)
        self.assertNotIn('"$@"', control)


if __name__ == "__main__":
    unittest.main()
