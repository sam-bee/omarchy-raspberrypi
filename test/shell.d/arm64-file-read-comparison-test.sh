#!/bin/bash

set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

require_command python3
require_command timeout
probe="$ROOT/docs/deployment/maintenance/compare-file-reads.py"
[[ -x $probe ]] || fail "the file read comparison probe is executable"
python3 -m py_compile "$probe"
pass "the file read comparison probe parses"

if (( EUID == 0 )); then
  fail "fixture tests require an ordinary non-root user"
fi

test_tmp=$(mktemp -d /tmp/omarchy-file-read-test.XXXXXX)
trap 'rm -rf -- "$test_tmp"' EXIT
fixture="$test_tmp/fixture"
mkdir -p "$fixture"
manifest="$test_tmp/manifest.json"

python3 - "$fixture" "$manifest" <<'PY'
import hashlib
import json
import os
import sys

root, manifest = sys.argv[1:]
files = []
for name, size in (("aligned.bin", 8192), ("nonaligned.bin", 5001), ("empty.bin", 0)):
    data = bytes((index * 37 + 11) % 256 for index in range(size))
    with open(os.path.join(root, name), "wb") as stream:
        stream.write(data)
    files.append({"path": name, "size": size, "sha256": hashlib.sha256(data).hexdigest()})
with open(manifest, "w", encoding="utf-8") as stream:
    json.dump({"version": 1, "files": files, "provenance": {"fixture": True}}, stream)
PY

good_output="$test_tmp/good-output"
python3 "$probe" --root "$fixture" --manifest "$manifest" --output "$good_output" --rounds 1 --allow-writable-fixture >"$test_tmp/good.jsonl" || fail "aligned and nonaligned fixtures compare successfully"
python3 - "$test_tmp/good.jsonl" "$good_output" <<'PY'
import json
import os
import stat
import sys

records = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
results = {record["path"]: record for record in records if record.get("type") == "result"}
assert all(record["status"] == "pass" for record in results.values())
assert results["aligned.bin"]["direct"]["requested_bytes"] == results["aligned.bin"]["size"]
assert results["nonaligned.bin"]["direct"]["requested_bytes"] > results["nonaligned.bin"]["size"]
assert results["nonaligned.bin"]["direct"]["eof_tail_validated"] is True
assert records[-1]["type"] == "summary" and records[-1]["status"] == "pass"
assert stat.S_IMODE(os.stat(sys.argv[2]).st_mode) == 0o700
assert os.path.exists(os.path.join(sys.argv[2], "results.jsonl"))
assert os.path.exists(os.path.join(sys.argv[2], "manifest.json"))
PY
pass "aligned and nonaligned O_DIRECT reads validate EOF tails and expected hashes"

wrong_manifest="$test_tmp/wrong-hash.json"
python3 - "$manifest" "$wrong_manifest" <<'PY'
import json
import sys

value = json.load(open(sys.argv[1], encoding="utf-8"))
value["files"][1]["sha256"] = "0" * 64
json.dump(value, open(sys.argv[2], "w", encoding="utf-8"))
PY
wrong_output="$test_tmp/wrong-output"
set +e
python3 "$probe" --root "$fixture" --manifest "$wrong_manifest" --output "$wrong_output" --rounds 1 --allow-writable-fixture >"$test_tmp/wrong.jsonl"
wrong_rc=$?
set -e
(( wrong_rc != 0 )) || fail "wrong expected hash fails the comparison"
python3 - "$test_tmp/wrong.jsonl" "$wrong_output" "$fixture/nonaligned.bin" <<'PY'
import glob
import json
import os
import sys

records = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
result = next(record for record in records if record.get("type") == "result" and record.get("status") == "mismatch")
assert result["status"] == "mismatch" and result["expected_match"] is False
retained = result["retained"]
assert len(retained) == 3
expected = open(sys.argv[3], "rb").read()
assert all(open(path, "rb").read() == expected for path in retained)
assert records[-1]["type"] == "summary" and records[-1]["status"] == "mismatch"
assert len(glob.glob(os.path.join(sys.argv[2], "*.bin"))) == 3
PY
pass "the first mismatch retains all three complete streams"

malformed="$test_tmp/malformed.json"
printf '%s\n' '{"version": 1, "files": [}' >"$malformed"
set +e
python3 "$probe" --root "$fixture" --manifest "$malformed" --output "$test_tmp/malformed-output" --rounds 1 --allow-writable-fixture >"$test_tmp/malformed.jsonl"
malformed_rc=$?
set -e
(( malformed_rc != 0 )) || fail "malformed manifests fail"
python3 - "$test_tmp/malformed.jsonl" <<'PY'
import json
import sys

records = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
assert records[-1]["type"] == "summary" and records[-1]["status"] == "error"
assert any(record.get("error", {}).get("kind") == "manifest_error" for record in records)
PY
pass "malformed manifests produce structured errors"

escape_manifest="$test_tmp/escape.json"
python3 - "$escape_manifest" <<'PY'
import json
import sys

json.dump({"version": 1, "files": [{"path": "../outside", "size": 0, "sha256": "0" * 64}]}, open(sys.argv[1], "w", encoding="utf-8"))
PY
set +e
python3 "$probe" --root "$fixture" --manifest "$escape_manifest" --output "$test_tmp/escape-output" --rounds 1 --allow-writable-fixture >"$test_tmp/escape.jsonl"
escape_rc=$?
set -e
(( escape_rc != 0 )) || fail "path escape manifests fail"
python3 - "$test_tmp/escape.jsonl" <<'PY'
import json
import sys

records = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
assert any(record.get("error", {}).get("kind") == "manifest_path" for record in records)
PY
pass "path escapes are rejected before opening input files"

set +e
python3 "$probe" --root "$fixture" --manifest "$manifest" --output "$test_tmp/budget-output" --rounds 1 --max-total-bytes 1 --allow-writable-fixture >"$test_tmp/budget.jsonl"
budget_rc=$?
set -e
(( budget_rc != 0 )) || fail "an aggregate read budget refusal fails"
python3 - "$test_tmp/budget.jsonl" <<'PY'
import json
import sys

records = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
assert any(record.get("error", {}).get("kind") == "read_budget_exceeded" for record in records)
PY
pass "aggregate read budgets are enforced before reads"

set +e
python3 "$probe" --root "$fixture" --manifest "$manifest" --output "$test_tmp/no-fixture-flag" --rounds 1 >"$test_tmp/mount.jsonl"
mount_rc=$?
set -e
(( mount_rc != 0 )) || fail "writable fixtures are refused without the explicit fixture flag"
python3 - "$test_tmp/mount.jsonl" <<'PY'
import json
import sys

records = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
assert any(record.get("error", {}).get("kind") == "target_mount_not_readonly" for record in records)
PY
pass "writable target mounts require the non-root local-fixture flag"

outside="$test_tmp/outside.bin"
printf '%s' outside >"$outside"
ln -s "$outside" "$fixture/escape-link"
symlink_manifest="$test_tmp/symlink.json"
python3 - "$symlink_manifest" <<'PY'
import json
import sys

json.dump({"version": 1, "files": [{"path": "escape-link", "size": 6, "sha256": "0" * 64}]}, open(sys.argv[1], "w", encoding="utf-8"))
PY
set +e
python3 "$probe" --root "$fixture" --manifest "$symlink_manifest" --output "$test_tmp/symlink-output" --rounds 1 --allow-writable-fixture >"$test_tmp/symlink.jsonl"
symlink_rc=$?
set -e
(( symlink_rc != 0 )) || fail "symlink input paths fail"
python3 - "$test_tmp/symlink.jsonl" <<'PY'
import json
import sys

records = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
assert any(record.get("error", {}).get("kind") == "open_error" for record in records)
PY
pass "symlink components cannot escape the target root"

set +e
python3 "$probe" --root "$fixture" --manifest "$manifest" --output "$fixture/output-inside" --rounds 1 --allow-writable-fixture >"$test_tmp/output-inside.jsonl"
inside_rc=$?
set -e
(( inside_rc != 0 )) || fail "output directories inside the target root fail"
python3 - "$test_tmp/output-inside.jsonl" <<'PY'
import json
import sys

records = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
assert any(record.get("error", {}).get("kind") == "output_inside_root" for record in records)
PY
pass "evidence output is required outside the target root"

mkfifo "$fixture/input.fifo"
fifo_manifest="$test_tmp/fifo.json"
python3 - "$fifo_manifest" <<'PY'
import json
import sys

json.dump({"version": 1, "files": [{"path": "input.fifo", "size": 0, "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}]}, open(sys.argv[1], "w", encoding="utf-8"))
PY
set +e
timeout --signal=TERM 5s python3 "$probe" --root "$fixture" --manifest "$fifo_manifest" --output "$test_tmp/fifo-output" --rounds 1 --allow-writable-fixture >"$test_tmp/fifo.jsonl"
fifo_rc=$?
set -e
(( fifo_rc != 124 )) || fail "FIFO input does not hang the read probe"
(( fifo_rc != 0 )) || fail "FIFO input is rejected as a non-regular file"
python3 - "$test_tmp/fifo.jsonl" <<'PY'
import json
import sys

records = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
assert any(record.get("error", {}).get("kind") in {"open_error", "not_regular_file"} for record in records)
PY
pass "non-regular FIFO inputs fail without blocking"
