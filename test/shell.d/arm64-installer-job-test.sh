#!/bin/bash

set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

image_dir="$ROOT/install/arm64/installer-image"
job_test="$image_dir/test-installer-job.py"
client="$image_dir/omarchy-pi-install"
control="$image_dir/installer-control"
unit="$image_dir/omarchy-pi-install.service"

[[ -x "$client" ]] || fail "terminal installer client is executable"
pass "terminal installer client is executable"
[[ -x "$control" ]] || fail "privileged installer control is executable"
pass "privileged installer control is executable"

if "$client" unexpected >"$ROOT/.installer-job-client.out" 2>&1; then
  fail "terminal installer client rejects arguments"
fi
grep -q 'accepts no arguments' "$ROOT/.installer-job-client.out" || fail "terminal installer client reports its fixed interface"
rm -f "$ROOT/.installer-job-client.out"
pass "terminal installer client rejects arguments"

if "$control" unexpected >"$ROOT/.installer-job-control.out" 2>&1; then
  fail "privileged installer control rejects arguments"
fi
grep -q 'accepts no arguments' "$ROOT/.installer-job-control.out" || fail "privileged installer control reports its fixed interface"
rm -f "$ROOT/.installer-job-control.out"
pass "privileged installer control rejects arguments"

grep -q '^Restart=no$' "$unit" || fail "installer worker does not auto-restart"
! grep -q '^\[Install\]$' "$unit" || fail "installer worker is not enabled at boot"
grep -q 'StandardInput=null' "$unit" || fail "installer worker does not inherit a terminal"
pass "installer worker is manual and session-independent"

python3 "$job_test" || fail "installer job Python contract tests pass"
pass "installer job Python contract tests pass"

python3 "$image_dir/test-installer-ui.py" || fail "guided installer UI contract tests pass"
pass "guided installer UI contract tests pass"
