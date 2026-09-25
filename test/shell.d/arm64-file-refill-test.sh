#!/bin/bash

set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

require_command python3
runner="$ROOT/docs/deployment/maintenance/compare-file-refills.py"
[[ -x $runner ]] || fail "the file refill runner is executable"
python3 -m py_compile "$runner"
pass "the file refill runner parses"

if (( EUID == 0 )); then
  fail "refill unit fixtures require an ordinary non-root user"
fi

python3 - "$runner" <<'PY'
import contextlib
import hashlib
import importlib.util
import io
import json
import mmap
import os
import signal
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


runner_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("compare_file_refills", runner_path)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"ok - {message}")


def raises_kind(function, kind, message):
    try:
        function()
    except (runner.RunnerError, runner.HELPER.ProbeError) as error:
        check(error.kind == kind, message + f" (kind={error.kind!r})")
        return
    raise AssertionError(message + " (did not raise)")


with tempfile.TemporaryDirectory(prefix="omarchy-file-refill-test.", dir="/tmp") as tmp:
    tmp_path = Path(tmp)
    target = tmp_path / "target.bin"
    target.write_bytes(bytes((index * 19 + 3) % 251 for index in range(4 * mmap.PAGESIZE)))
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    root = tmp_path / "root"
    root.mkdir()
    (root / "target.bin").symlink_to(target)

    # The production command has no fixture bypass.  This catches accidental
    # reintroduction of the comparator's writable-fixture escape hatch.
    source = runner_path.read_text(encoding="utf-8")
    check("--allow-writable-fixture" not in source, "production runner has no writable-fixture flag")
    check("--fixture" not in source, "production runner has no generic fixture flag")
    raises_kind(lambda: runner.HELPER.check_mount(os.environ["ROOT"], False), "target_mount_not_readonly", "writable roots are refused without a fixture bypass")
    check("signal.signal(signal.SIGTERM" in source and '"type": "summary"' in source, "SIGTERM handling has a best-effort summary path")
    check("SIGKILL" not in source, "runner does not claim to handle uncatchable SIGKILL")

    # Root and initial-user-namespace gates are separate safety boundaries.
    raises_kind(runner.check_root_context, "root_required", "ordinary users cannot run the refill runner")
    with mock.patch.object(runner.os, "geteuid", return_value=0), mock.patch.object(runner, "_initial_user_namespace", return_value=False):
        raises_kind(runner.check_root_context, "initial_user_namespace_required", "nested user namespaces fail closed even when euid is mocked to root")

    # The literal / root target guard cannot be reached through the normal
    # non-root command, so exercise only that guard with the root-context check
    # mocked.  This keeps the test hook narrowly scoped to the documented root
    # namespace boundary.
    root_output = tmp_path / "root-output"
    with mock.patch.object(runner, "check_root_context", return_value={"euid": os.geteuid(), "egid": os.getegid(), "initial_user_namespace": True, "cap_fowner": True}):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = runner.main(["--root", "/", "--manifest", str(tmp_path / "missing.json"), "--output", str(root_output), "--max-seconds", "1"])
        records = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        check(result != 0 and any(record.get("error", {}).get("kind") == "root_guard" for record in records), "root target is refused before opening files")

    # Symlink components are rejected by the shared openat/no-follow helper.
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        raises_kind(lambda: runner.HELPER.open_relative(root_fd, "target.bin", os.O_RDONLY), "open_error", "manifest symlink escape is refused")
    finally:
        os.close(root_fd)

    # Mapping protection is inode based and must see the real process map.
    fd = os.open(target, os.O_RDONLY)
    mapping = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
    try:
        st = os.fstat(fd)
        scan = runner.scan_proc_maps(st.st_dev, st.st_ino)
        check(scan["mapped"] is True and any(match["pid"] == os.getpid() for match in scan["matches"]), "mapped inode prevents the refill phase")
    finally:
        mapping.close()
        os.close(fd)

    # Unreadable maps are an abort condition.  In particular this also catches
    # a missing errno import in the vanishing/unreadable-process branch.
    original_open = runner.os.open
    def deny_maps(path, flags, *args, **kwargs):
        if str(path).endswith("/1234/maps"):
            raise PermissionError(13, "fixture maps unreadable")
        return original_open(path, flags, *args, **kwargs)
    fake_process = SimpleNamespace(name="1234")
    with mock.patch.object(runner.os, "scandir", return_value=[fake_process]), mock.patch.object(runner.os, "open", side_effect=deny_maps):
        raises_kind(lambda: runner.scan_proc_maps(0, 1), "maps_visibility", "unreadable process maps fail closed")

    # posix_fadvise errors and non-zero libc-style returns are not success.
    with mock.patch.object(runner.os, "posix_fadvise", side_effect=OSError(5, "fixture fadvise failure")):
        raises_kind(lambda: runner.fadvise_dontneed(7, 4096), "fadvise_error", "fadvise errors abort the refill")
    with mock.patch.object(runner.os, "posix_fadvise", return_value=22):
        raises_kind(lambda: runner.fadvise_dontneed(7, 4096), "fadvise_error", "non-zero fadvise returns abort the refill")

    # Residency errors cannot be treated as a cold pass, and a non-zero
    # post-fadvise residency count is explicitly not a cold range.
    with mock.patch.object(runner, "_mincore_call", side_effect=runner.RunnerError("mincore_error", "fixture mincore failure")):
        raises_kind(lambda: runner.residency(7, 4096), "mincore_error", "mincore failure is fail closed")
    check("if not cold:" in source and "cold_range_unsupported" in source, "partial eviction cannot be reported as a cold refill")
    raises_kind(lambda: runner.check_deadline(runner.time.monotonic() - 1), "time_limit", "expired wall-clock deadline stops the runner")
    check("target_planned = (1 + REFILL_CYCLES) * one_cycle" in source, "aggregate target budget includes baseline and every refill cycle")

    # Metadata drift is checked against the pinned identity after the path is
    # reopened.  Patch only the mount check: this is a unit fixture for the
    # identity transition, not a writable-target production bypass.
    file_path = root / "stable.bin"
    file_path.write_bytes(b"before")
    first = os.stat(file_path)
    entry = runner.HELPER.Entry("stable.bin", first.st_size, hashlib.sha256(b"before").hexdigest(), mmap.PAGESIZE, mmap.PAGESIZE)
    pinned = runner.PinnedEntry(entry, runner.HELPER.stable(first), runner.HELPER.stat_record(first), first.st_size * 3)
    replacement = root / "replacement.bin"
    replacement.write_bytes(b"after!")
    file_path.unlink()
    replacement.rename(file_path)
    runner.ROOT_CONTEXT = {"euid": os.geteuid(), "egid": os.getegid(), "initial_user_namespace": True, "cap_fowner": False}
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with mock.patch.object(runner.HELPER, "check_fd_mount"):
            raises_kind(lambda: runner.open_pinned(root_fd, pinned), "pinned_identity_changed", "metadata and inode drift aborts a refill")
    finally:
        os.close(root_fd)

    # The aggregate byte cap is checked before the residency control or any
    # target advisory call, and includes all four target passes.
    budget_root = tmp_path / "budget-root"
    budget_root.mkdir()
    budget_file = budget_root / "sample.bin"
    budget_file.write_bytes(b"budget")
    budget_manifest = tmp_path / "budget-manifest.json"
    budget_manifest.write_text(json.dumps({"version": 1, "files": [{"path": "sample.bin", "size": 6, "sha256": hashlib.sha256(b"budget").hexdigest()}]}), encoding="utf-8")
    budget_output = tmp_path / "budget-output"
    budget_stat = os.stat(budget_file)
    budget_entry = runner.HELPER.Entry("sample.bin", 6, hashlib.sha256(b"budget").hexdigest(), mmap.PAGESIZE, mmap.PAGESIZE)
    budget_pinned = runner.PinnedEntry(budget_entry, runner.HELPER.stable(budget_stat), runner.HELPER.stat_record(budget_stat), 100)
    with mock.patch.object(runner, "check_root_context", return_value={"euid": os.geteuid(), "egid": os.getegid(), "initial_user_namespace": True, "cap_fowner": False}), \
         mock.patch.object(runner.HELPER, "check_mount"), \
         mock.patch.object(runner.HELPER, "check_fd_mount"), \
         mock.patch.object(runner, "require_disk_output_parent", return_value={"parent": str(tmp_path), "filesystem_type": "ext4"}), \
         mock.patch.object(runner, "load_entries", return_value=([budget_pinned], 100)), \
         mock.patch.object(runner, "run_residency_control") as control, \
         mock.patch.object(runner, "fadvise_dontneed") as fadvise:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            budget_result = runner.main(["--root", str(budget_root), "--manifest", str(budget_manifest), "--output", str(budget_output), "--max-seconds", "2", "--max-total-bytes", "400"])
        records = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        check(budget_result != 0 and control.call_count == 0 and fadvise.call_count == 0, "cumulative byte cap stops before residency or fadvise")
        check(any(record.get("error", {}).get("kind") == "read_budget_exceeded" for record in records), "cumulative byte cap reports read_budget_exceeded")

    # A baseline mismatch is a hard pre-refill gate.  Mock only the I/O and
    # mount boundary here; the shared helper's independent full-retention
    # behavior is covered by arm64-file-read-comparison-test.sh.
    baseline_root = tmp_path / "baseline-root"
    baseline_root.mkdir()
    baseline_file = baseline_root / "sample.bin"
    baseline_file.write_bytes(b"baseline")
    baseline_manifest = tmp_path / "baseline-manifest.json"
    baseline_manifest.write_text(json.dumps({"version": 1, "files": [{"path": "sample.bin", "size": 8, "sha256": "0" * 64}]}), encoding="utf-8")
    baseline_output = tmp_path / "baseline-output"
    baseline_stat = os.stat(baseline_file)
    baseline_entry = runner.HELPER.Entry("sample.bin", 8, "0" * 64, mmap.PAGESIZE, mmap.PAGESIZE)
    baseline_pinned = runner.PinnedEntry(baseline_entry, runner.HELPER.stable(baseline_stat), runner.HELPER.stat_record(baseline_stat), 24)
    retained = [str(baseline_output / "file-001-round-0-buffered1.bin"), str(baseline_output / "file-001-round-0-direct.bin"), str(baseline_output / "file-001-round-0-buffered2.bin")]
    baseline_stat_record = runner.HELPER.stat_record(baseline_stat)
    mismatch_record = {"type": "result", "status": "mismatch", "path": "sample.bin", "round": 0, "retained": retained, "fstat": {name: baseline_stat_record for name in ("before_buffered", "before_direct", "after_buffered", "after_direct")}}
    with mock.patch.object(runner, "check_root_context", return_value={"euid": os.geteuid(), "egid": os.getegid(), "initial_user_namespace": True, "cap_fowner": False}), \
         mock.patch.object(runner.HELPER, "check_mount"), \
         mock.patch.object(runner.HELPER, "check_fd_mount"), \
         mock.patch.object(runner, "require_disk_output_parent", return_value={"parent": str(tmp_path), "filesystem_type": "ext4"}), \
         mock.patch.object(runner, "load_entries", return_value=([baseline_pinned], 24)), \
         mock.patch.object(runner, "run_residency_control", return_value={"disk_backed": True, "size": 4 * mmap.PAGESIZE}), \
         mock.patch.object(runner, "scan_proc_maps", return_value={"mapped": False}), \
         mock.patch.object(runner, "open_pinned", side_effect=lambda *_args: (os.open(baseline_file, os.O_RDONLY), baseline_stat, {})), \
         mock.patch.object(runner.HELPER, "compare_entry", return_value=mismatch_record), \
         mock.patch.object(runner, "fadvise_dontneed") as fadvise:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            baseline_result = runner.main(["--root", str(baseline_root), "--manifest", str(baseline_manifest), "--output", str(baseline_output), "--max-seconds", "2"])
        records = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        check(baseline_result != 0 and fadvise.call_count == 0, "baseline mismatch stops before any file-specific fadvise")
        check(any(record.get("error", {}).get("kind") == "baseline_mismatch" for record in records), "baseline mismatch is classified explicitly")
        baseline_results = [record for record in records if record.get("type") == "result"]
        check(baseline_results and baseline_results[0].get("retained") == retained, "baseline mismatch preserves all three stream paths")

    # The baseline acknowledgement is a durable checkpoint gate.  Invalid or
    # missing ACK input must fail before a caller can proceed to fadvise.
    ack_dir = tmp_path / "ack"
    ack_dir.mkdir()
    (ack_dir / "results.jsonl").write_bytes(b"baseline\n")
    token = hashlib.sha256(b"baseline\n").hexdigest()
    logger = SimpleNamespace(fd=-1)
    with mock.patch.object(runner, "sync_logger"), mock.patch.object(runner.HELPER, "emit"):
        with mock.patch.object(runner.sys, "stdin", io.StringIO(f"ACK {token}\n")):
            with mock.patch("select.select", return_value=([runner.sys.stdin], [], [])):
                acknowledged = runner.await_baseline_ack(str(ack_dir), logger, "manifest", time_deadline := (runner.time.monotonic() + 2))
                check(acknowledged == token, "valid baseline ACK unlocks the refill gate")
        with mock.patch.object(runner.sys, "stdin", io.StringIO("ACK wrong\n")):
            with mock.patch("select.select", return_value=([runner.sys.stdin], [], [])):
                raises_kind(lambda: runner.await_baseline_ack(str(ack_dir), logger, "manifest", runner.time.monotonic() + 2), "baseline_ack_invalid", "wrong baseline ACK blocks fadvise")
        with mock.patch.object(runner.sys, "stdin", io.StringIO("")):
            with mock.patch("select.select", return_value=([], [], [])):
                raises_kind(lambda: runner.await_baseline_ack(str(ack_dir), logger, "manifest", runner.time.monotonic() + 2), "baseline_ack_timeout", "missing baseline ACK blocks fadvise")

    # Hard caps remain bounded even when callers pass excessive values.
    for option, value in (("--max-seconds", "181"), ("--max-total-bytes", str(384 * 1024 * 1024 + 1))):
        try:
            runner.parse_args(["--root", "/offline", "--manifest", "manifest", "--output", "output", option, value])
        except SystemExit as error:
            check(error.code == 2, f"{option} cannot exceed its hard cap")
        else:
            raise AssertionError(f"{option} exceeded its hard cap")

    # TERM requests a bounded, best-effort stop.  SIGKILL has no handler and is
    # deliberately outside this unit test's claims.
    runner._stop_signal = None
    try:
        runner._signal_stop(signal.SIGTERM, None)
    except runner.RunnerError as error:
        check(error.kind == "signal" and error.details["signal"] == signal.SIGTERM, "SIGTERM produces a structured partial-stop error")
    else:
        raise AssertionError("SIGTERM did not stop the runner")

print("ok - refill runner adversarial unit checks")
PY
