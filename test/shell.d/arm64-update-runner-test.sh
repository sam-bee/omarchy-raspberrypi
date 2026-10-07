#!/bin/bash

set -euo pipefail

source "$(dirname "$0")/base-test.sh"

runner="$ROOT/test/arm64/test-update-runner.py"
[[ -f $runner ]] || fail "durable Pi update runner tests exist"

python3 "$runner" || fail "durable Pi update runner contract tests pass"
pass "durable Pi update runner contract tests pass"

package_runner="$ROOT/test/arm64/test-update-packages.py"
[[ -f $package_runner ]] || fail "packaged ARM update contract tests exist"
python3 "$package_runner" || fail "packaged ARM update contract tests pass"
pass "packaged ARM update contract tests pass"

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
cp "$ROOT/install/arm64/update_packages.py" "$pi_root/install/arm64/update_packages.py"
pi_output=$(PATH="$fake_bin:$PATH" OMARCHY_PATH="$pi_root" "$ROOT/bin/omarchy-update" --help 2>&1) ||
  fail "update shell dispatch reaches the Pi Python runner on aarch64"
grep -Fq -- "Run Pi updates" <<<"$pi_output" || fail "Pi dispatch reaches update.py argument parser"
pass "update shell dispatch reaches the Pi Python runner on aarch64"

packaged_root="$test_tmp/packaged"
mkdir -p "$packaged_root/config" "$packaged_root/install/arm64"
touch "$packaged_root/version" "$packaged_root/install/arm64/update_packages.py"
printf '%s\n' '{"schema_version":1,"runtime_mode":"packaged","source_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}' >"$packaged_root/.omarchy-pi-packaged.json"
cat >"$packaged_root/install/arm64/update.py" <<'PY'
import sys
print("packaged-dispatch", *sys.argv[1:])
PY
cat >"$fake_bin/pacman" <<'SH'
#!/bin/bash
if [[ $1 == -Qo ]]; then
  case "$3" in
    */version) printf 'omarchy 4-1 owns %s\n' "$3" ;;
    */config) printf 'omarchy-settings 4-1 owns %s\n' "$3" ;;
    *) exit 1 ;;
  esac
  exit 0
fi
exit 1
SH
chmod +x "$fake_bin/pacman"
packaged_output=$(PATH="$fake_bin:$PATH" OMARCHY_PATH="$packaged_root" OMARCHY_PI_TESTING=1 OMARCHY_PI_TEST_RUNTIME_ROOT="$packaged_root" "$ROOT/bin/omarchy-update" --status 2>&1) ||
  fail "update shell dispatch reaches packaged ARM runner"
grep -Fq -- "packaged-dispatch --packaged --status" <<<"$packaged_output" || fail "packaged ARM dispatch passes its mode marker"
pass "update shell dispatch reaches packaged ARM runner"
