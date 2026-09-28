#!/bin/bash

set -euo pipefail

source "$(dirname "$0")/base-test.sh"

runner="$ROOT/test/arm64/test-update-runner.py"
[[ -f $runner ]] || fail "durable Pi update runner tests exist"

python3 "$runner" || fail "durable Pi update runner contract tests pass"
pass "durable Pi update runner contract tests pass"

test_tmp=$(mktemp -d)
trap 'rm -rf "$test_tmp"' EXIT

fake_bin="$test_tmp/bin"
mkdir -p "$fake_bin"
cat >"$fake_bin/uname" <<'SH'
#!/bin/bash
printf '%s\n' "armv7l"
SH
chmod +x "$fake_bin/uname"

set +e
non_pi_output=$(PATH="$fake_bin:$PATH" "$ROOT/bin/omarchy-update" --help 2>&1)
non_pi_status=$?
set -e
[[ $non_pi_status -eq 1 ]] || fail "update shell dispatch rejects unsupported ARM architectures"
grep -Fq "not supported on ARM" <<<"$non_pi_output" || fail "unsupported ARM dispatch explains the supported path"
pass "update shell dispatch rejects unsupported ARM architectures"

cat >"$fake_bin/uname" <<'SH'
#!/bin/bash
printf '%s\n' "aarch64"
SH
chmod +x "$fake_bin/uname"

pi_root="$test_tmp/pi"
mkdir -p "$pi_root/install/arm64"
touch "$pi_root/.omarchy-pi-source-commit"
cp "$ROOT/install/arm64/update.py" "$pi_root/install/arm64/update.py"
pi_output=$(PATH="$fake_bin:$PATH" OMARCHY_PATH="$pi_root" "$ROOT/bin/omarchy-update" --help 2>&1) ||
  fail "update shell dispatch reaches the Pi Python runner on aarch64"
grep -Fq -- "Run Pi updates" <<<"$pi_output" || fail "Pi dispatch reaches update.py argument parser"
pass "update shell dispatch reaches the Pi Python runner on aarch64"
