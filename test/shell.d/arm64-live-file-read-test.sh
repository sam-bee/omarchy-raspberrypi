#!/bin/bash

set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

require_command python3
runner="$ROOT/docs/deployment/maintenance/compare-live-file-reads.py"
[[ -x $runner ]] || fail "the live checkpoint runner is executable"
python3 -m py_compile "$runner"
pass "the live checkpoint runner parses"

if (( EUID == 0 )); then
  fail "live checkpoint fixture tests require an ordinary non-root user"
fi

python3 - "$runner" <<'PY'
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


runner_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("compare_live_file_reads", runner_path)
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
    except runner.ProbeError as error:
        check(error.kind == kind, message + f" (kind={error.kind!r})")
        return
    raise AssertionError(message + " (did not raise)")


def records_from(stdout):
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def run_checkpoint(root, output_base, manifest, phase="pre-desktop", *, direct_failure=None, read_buffered=None):
    args = [
        "--manifest", str(manifest),
        "--expected-boot-id", "fixture-boot",
        "--expected-root-uuid", "fixture-root",
        "--expected-kernel-release", "fixture-kernel",
        "--phase", phase,
    ]
    guard = {"mountpoint": "/", "fstype": "ext4", "source": "/dev/mapper/cryptroot", "options": "rw"}
    patches = [
        mock.patch.object(runner, "LIVE_ROOT", str(root)),
        mock.patch.object(runner, "OUTPUT_BASE", str(output_base)),
        mock.patch.object(runner, "guard_live_root", return_value=guard),
    ]
    if direct_failure is not None:
        patches.append(mock.patch.object(runner.comparison, "read_direct", side_effect=direct_failure))
    if read_buffered is not None:
        patches.append(mock.patch.object(runner.comparison, "read_buffered", side_effect=read_buffered))
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = runner.main(args)
    records = records_from(stdout)
    summary = next(record for record in records if record["type"] == "summary")
    return rc, records, summary, Path(summary["output"])


with tempfile.TemporaryDirectory(prefix="omarchy-live-checkpoint.") as tmp:
    tmp_path = Path(tmp)
    root = tmp_path / "root"
    root.mkdir()
    output_base = tmp_path / "output-base"
    files = {}
    for name, size in (("aligned.bin", 4096), ("tail.bin", 5001), ("third.bin", 8193)):
        data = bytes((index * 29 + 7) % 256 for index in range(size))
        path = root / name
        path.write_bytes(data)
        files[name] = data

    def manifest_for(entries):
        return json.dumps(
            {"version": 1, "files": [
                {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in entries
            ]},
            separators=(",", ":"),
        ).encode()

    good_manifest = tmp_path / "good.json"
    good_bytes = manifest_for(list(files.items()))
    good_manifest.write_bytes(good_bytes)

    # This is a real buffered/aligned O_DIRECT run against ordinary local files;
    # only the live-root context guard is mocked so a writable fixture is never
    # accepted by the production CLI.
    rc, records, summary, success_output = run_checkpoint(root, output_base, good_manifest)
    check(rc == 0 and summary["status"] == "pass", "three fixed manifest entries pass one live checkpoint")
    results = [record for record in records if record["type"] == "result"]
    check([record["path"] for record in results] == list(files), "the checkpoint preserves manifest order and performs one pass")
    check(all(record["status"] == "pass" and record["agreement"] and record["expected_match"] for record in results), "buffered and aligned O_DIRECT streams match the signed fixture")
    check(summary["planned_bytes"] > 0 and summary["max_planned_bytes"] == 64 * 1024 * 1024 and summary["max_seconds"] == 30.0, "the live checkpoint reports the 64 MiB and 30 second caps")
    check(summary["context"]["phase"] == "pre-desktop" and summary["context"]["root"] == "/", "phase and live-root context are recorded")
    check(stat.S_IMODE(success_output.stat().st_mode) == 0o700, "checkpoint output is private")
    check(stat.S_IMODE((success_output / "manifest.json").stat().st_mode) == 0o600 and (success_output / "results.jsonl").exists(), "manifest and JSONL evidence are retained")
    check(success_output.parent == output_base and success_output.name.startswith("checkpoint-"), "each run creates a new directory under the startup output base")

    # A wrong signed digest must retain all three complete streams and classify
    # the final summary as a mismatch rather than a generic runner error.
    wrong = json.loads(good_bytes)
    wrong["files"][0]["sha256"] = "0" * 64
    wrong_manifest = tmp_path / "wrong.json"
    wrong_manifest.write_text(json.dumps(wrong, separators=(",", ":")), encoding="utf-8")
    rc, records, summary, mismatch_output = run_checkpoint(root, output_base, wrong_manifest)
    result = next(record for record in records if record["type"] == "result")
    retained = [Path(path) for path in result["retained"]]
    check(rc != 0 and result["status"] == "mismatch" and len(retained) == 3, "a mismatch stops after the first file and retains three streams")
    check(all(path.exists() and path.read_bytes() == files["aligned.bin"] for path in retained), "retained mismatch streams preserve complete bytes")
    check(summary["status"] == "mismatch", "mismatch evidence is classified as mismatch in the summary")

    # A failure after the first buffered stream must leave partial evidence,
    # including the exact phase's three stream names.
    def fail_direct(*_args, **_kwargs):
        raise runner.ProbeError("direct_read_error", "fixture direct read failure")

    one_manifest = tmp_path / "one.json"
    one_manifest.write_bytes(manifest_for([("tail.bin", files["tail.bin"])]))
    rc, records, summary, partial_output = run_checkpoint(root, output_base, one_manifest, direct_failure=fail_direct)
    partial = sorted(partial_output.glob("*.partial.bin"))
    check(rc != 0 and any(record["type"] == "error" and record["error"]["kind"] == "direct_read_error" for record in records), "a direct-read failure stops the checkpoint")
    check(len(partial) == 3 and (partial_output / "file-001-round-1-buffered1.partial.bin").read_bytes() == files["tail.bin"], "partial stream retention preserves bytes read before failure")

    # fstat metadata is pinned across the complete three-stream pass.
    calls = 0
    original_read_buffered = runner.comparison.read_buffered

    def change_metadata(fd, offset, length):
        nonlocal_calls[0] += 1
        data = original_read_buffered(fd, offset, length)
        if nonlocal_calls[0] == 2:
            os.utime(root / "tail.bin", ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        return data

    nonlocal_calls = [0]
    rc, records, summary, identity_output = run_checkpoint(root, output_base, one_manifest, read_buffered=change_metadata)
    result = next(record for record in records if record["type"] == "result")
    check(rc != 0 and result["status"] == "error" and result["error"]["kind"] == "fstat_changed", "metadata drift is rejected after the pinned read")

    # Symlink components and FIFOs are refused during preflight without a
    # blocking open; path validation rejects traversal before touching the root.
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    (root / "escape.bin").symlink_to(outside)
    os.mkfifo(root / "input.fifo")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        symlink_manifest = json.dumps({"version": 1, "files": [{"path": "escape.bin", "size": 7, "sha256": "0" * 64}]}).encode()
        fifo_manifest = json.dumps({"version": 1, "files": [{"path": "input.fifo", "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}]}).encode()
        raises_kind(lambda: runner.plan_entries(symlink_manifest, root_fd, float("inf")), "open_error", "symlink input paths are refused")
        raises_kind(lambda: runner.plan_entries(fifo_manifest, root_fd, float("inf")), "not_regular_file", "FIFO input paths are refused without blocking")
    finally:
        os.close(root_fd)
    escape_manifest = json.dumps({"version": 1, "files": [{"path": "../outside", "size": 0, "sha256": "0" * 64}]}).encode()
    raises_kind(lambda: runner.comparison.load_manifest(escape_manifest), "manifest_path", "manifest path traversal is rejected")

    # The file-count and aggregate planned-byte limits fail before comparison.
    fourth_data = b"fourth"
    (root / "fourth.bin").write_bytes(fourth_data)
    four_manifest = manifest_for(list(files.items()) + [("fourth.bin", fourth_data)])
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        raises_kind(lambda: runner.plan_entries(four_manifest, root_fd, float("inf")), "manifest_file_count", "more than three manifest items are refused")
        budget_names = []
        for index in range(3):
            name = f"sparse-{index}.bin"
            with open(root / name, "wb") as stream:
                stream.truncate(24 * 1024 * 1024)
            budget_names.append((name, b""))
        budget_manifest = json.dumps({"version": 1, "files": [{"path": name, "size": 24 * 1024 * 1024, "sha256": "0" * 64} for name, _ in budget_names]}).encode()
        raises_kind(lambda: runner.plan_entries(budget_manifest, root_fd, float("inf")), "read_budget_exceeded", "the 64 MiB aggregate plan cap is enforced")
    finally:
        os.close(root_fd)

    # The production guard is root-only, and it runs before output creation.
    raises_kind(runner.check_root_context, "root_required", "ordinary users cannot run the live checkpoint")
    guard_order_base = tmp_path / "guard-order-output"
    with mock.patch.object(runner, "OUTPUT_BASE", str(guard_order_base)), mock.patch.object(runner, "guard_live_root", side_effect=runner.ProbeError("root_required", "fixture root guard")):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            guard_rc = runner.main(["--manifest", str(good_manifest), "--expected-boot-id", "b", "--expected-root-uuid", "r", "--expected-kernel-release", "k", "--phase", "pre-desktop"])
    check(guard_rc != 0 and not guard_order_base.exists(), "a failed live guard creates no output directory")

    # Existing output-base metadata is an invariant: refusal must not repair or
    # chmod a caller-owned path.  A new base and child are both mode 0700.
    existing_base = tmp_path / "existing-output-base"
    existing_base.mkdir(mode=0o755)
    os.chmod(existing_base, 0o755)
    with mock.patch.object(runner, "OUTPUT_BASE", str(existing_base)):
        raises_kind(runner.create_output, "retention_error", "an existing output base with the wrong mode is refused")
    check(stat.S_IMODE(existing_base.stat().st_mode) == 0o755, "output-base refusal leaves existing mode unchanged")

    # Guard identity failures are deterministic and independent of the local
    # fixture: only the context readers are mocked here.
    context = [
        mock.patch.object(runner, "check_root_context", return_value={"euid": 0, "egid": 0, "initial_user_namespace": True}),
        mock.patch.object(runner, "read_boot_id", return_value="boot-good"),
        mock.patch.object(runner, "read_root_mount", return_value={"mountpoint": "/", "fstype": "ext4", "source": "/dev/mapper/cryptroot", "options": "rw"}),
        mock.patch.object(runner.os, "uname", return_value=SimpleNamespace(release="kernel-good")),
        mock.patch.object(runner, "check_root_uuid"),
    ]
    with contextlib.ExitStack() as stack:
        for patch in context:
            stack.enter_context(patch)
        raises_kind(lambda: runner.guard_live_root("boot-wrong", "root-good", "kernel-good"), "boot_id_mismatch", "wrong boot identity is rejected")
        raises_kind(lambda: runner.guard_live_root("boot-good", "root-good", "kernel-wrong"), "kernel_release_mismatch", "wrong kernel identity is rejected")
    with mock.patch.object(runner, "check_root_context", return_value={"euid": 0, "egid": 0, "initial_user_namespace": True}), mock.patch.object(runner, "read_boot_id", return_value="boot-good"), mock.patch.object(runner, "read_root_mount", return_value={"mountpoint": "/", "fstype": "xfs", "source": "/dev/mapper/cryptroot", "options": "rw"}), mock.patch.object(runner.os, "uname", return_value=SimpleNamespace(release="kernel-good")):
        raises_kind(lambda: runner.guard_live_root("boot-good", "root-wrong", "kernel-good"), "root_fstype_mismatch", "wrong root filesystem identity is rejected")
    with mock.patch.object(runner.os.path, "islink", return_value=True), mock.patch.object(runner.os.path, "realpath", side_effect=["/dev/mapper/other", "/dev/mapper/cryptroot"]):
        raises_kind(lambda: runner.check_root_uuid("deadbeef-dead-beef-dead-beefdeadbeef"), "root_uuid_mismatch", "wrong root UUID link is rejected")

    # The production wrapper has no fixture bypass or cache/mount/target-exec
    # side effects; its only writable output is the evidence directory.
    source = runner_path.read_text(encoding="utf-8")
    check("--allow-writable-fixture" not in source and "posix_fadvise" not in source and "drop_caches" not in source, "live wrapper has no writable-fixture or cache-eviction control")
    check("subprocess" not in source and "os.system" not in source and "execve" not in source and "\nmount(" not in source, "live wrapper does not mount or execute target commands")
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            runner.parse_args(["--manifest", "x", "--expected-boot-id", "b", "--expected-root-uuid", "r", "--expected-kernel-release", "k", "--phase", "pre-desktop", "--allow-writable-fixture"])
    except SystemExit as error:
        check(error.code == 2, "live CLI rejects a writable-fixture option")
    else:
        raise AssertionError("live CLI accepted a writable-fixture option")

print("ok - live checkpoint fixture coverage complete")
PY

pass "live checkpoint fixture behavior is covered"
