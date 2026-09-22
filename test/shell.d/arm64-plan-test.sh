#!/bin/bash

set -euo pipefail

source "$(dirname "$0")/base-test.sh"

require_command python3

planner="$ROOT/install/arm64/plan.py"
wrapper="$ROOT/bin/omarchy-install-plan"
[[ -f $planner ]] || fail "ARM64 planner exists" "$planner"
[[ -x $wrapper ]] || fail "ARM64 plan wrapper is executable" "$wrapper"
[[ -f $ROOT/install/arm64/packages.tsv ]] || fail "ARM64 base policy exists"
[[ -f $ROOT/install/arm64/packages-extra.tsv ]] || fail "ARM64 extra policy exists"

test_tmp=$(mktemp -d)
trap 'rm -rf "$test_tmp"' EXIT

stub_bin="$test_tmp/sentinel-bin"
sentinel_log="$test_tmp/sentinel.log"
mkdir -p "$stub_bin"

# A plan is a read-only local description. Put mutating or remote commands at
# the front of PATH so an accidental subprocess is visible and fails loudly.
for command in sudo pacman systemctl ssh; do
  printf '%s\n' '#!/bin/bash' \
    'printf "%s %s\n" "\${0##*/}" "$*" >>"$ARM64_PLAN_SENTINEL_LOG"' \
    'exit 97' >"$stub_bin/$command"
  chmod +x "$stub_bin/$command"
done

original_path="$PATH"
plan() {
  ARM64_PLAN_SENTINEL_LOG="$sentinel_log" \
  OMARCHY_PATH="$ROOT" \
  PATH="$stub_bin:$original_path" \
    "$wrapper" "$@"
}

expect_rejected() {
  local description="$1"
  shift
  local output

  if output=$(plan "$@" 2>&1); then
    fail "$description" "command unexpectedly succeeded: $*"
  fi
  [[ -n $output ]] || fail "$description reports an error"
  pass "$description"
}

json_contract() {
  local json_path="$1"
  local profile="$2"

  if ! python3 - "$json_path" "$profile" "$ROOT" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
profile = sys.argv[2]
root = Path(sys.argv[3])
data = json.loads(path.read_text())

def check(condition, message):
    if not condition:
        raise SystemExit(message)

check(data.get("schema_version") == 1, "schema_version is not 1")
check(data.get("mode") == "plan-only", "mode is not plan-only")
check(data.get("baseline") == "947e2fc002d6831c7888b29b5761d59d29e69727", "baseline is not pinned")
check(data.get("ready_to_apply") is False, "plan is marked ready to apply")
check(data.get("allowed_system_changes") == [], "allowed_system_changes is not empty")
check(data.get("target") == {"architecture": "aarch64", "profile": profile, "source": "explicit"}, "explicit target metadata is wrong")
check(isinstance(data.get("preserve"), list) and data["preserve"], "preserve policy is empty")
check(isinstance(data.get("blockers"), list) and data["blockers"], "blockers are empty")

packages = data.get("packages")
check(isinstance(packages, list) and packages, "package policy is empty")
actions = {"candidate", "replace", "defer", "exclude"}
names = []
for row in packages:
    check(set(("package", "action", "replacement", "reason")) <= set(row), "package row is missing a required field")
    check(isinstance(row["package"], str) and row["package"], "package row has no package name")
    check(row["action"] in actions, f"invalid package action: {row['action']!r}")
    check(isinstance(row["reason"], str) and row["reason"].strip(), f"missing reason for {row['package']}")
    if row["action"] == "replace":
        check(isinstance(row["replacement"], str) and row["replacement"] and row["replacement"] != row["package"], f"replacement missing for {row['package']}")
    else:
        check(row["replacement"] == "-", f"non-replacement row has a replacement: {row['package']}")
    names.append(row["package"])
check(len(names) == len(set(names)), "package policy output contains duplicate rows")

base = []
for line in (root / "install/omarchy-base.packages").read_text().splitlines():
    name = line.split("#", 1)[0].strip()
    if name:
        base.append(name)
check(set(base) <= set(names), "a base package is missing from the plan")
PY
  then
    fail "$profile JSON plan contract" "$(python3 -m json.tool "$json_path" 2>&1 || cat "$json_path")"
  fi
  pass "$profile JSON plan contract"
}

rpi_json="$test_tmp/rpi5.json"
if ! (cd /tmp && plan --target rpi5 --format json >"$rpi_json"); then
  fail "rpi5 JSON plan succeeds from /tmp"
fi
json_contract "$rpi_json" rpi5

# Re-running from a different cwd must not change a plan whose source is the
# pinned checkout. This catches accidental relative-path reads in the wrapper
# or planner.
rpi_json_again="$test_tmp/rpi5-again.json"
if ! (cd "$ROOT" && plan --target rpi5 --format json >"$rpi_json_again"); then
  fail "rpi5 JSON plan succeeds from the checkout"
fi
cmp -s "$rpi_json" "$rpi_json_again" || fail "JSON output is deterministic independent of cwd"
pass "JSON output is deterministic independent of cwd"

arm_json="$test_tmp/arm64.json"
if ! plan --target arm64 --format json >"$arm_json"; then
  fail "generic arm64 JSON plan succeeds"
fi
json_contract "$arm_json" arm64
if ! python3 - "$arm_json" <<'PY'
import json
import sys
assert all(row["package"] != "vulkan-broadcom" for row in json.load(open(sys.argv[1]))["packages"])
PY
then
  fail "generic arm64 plan omits Pi-only Vulkan candidate"
fi
pass "generic arm64 plan omits Pi-only Vulkan candidate"

text_output="$test_tmp/plan.txt"
if ! plan --target rpi5 --format text >"$text_output"; then
  fail "text plan succeeds"
fi
grep -Fq "Omarchy ARM64 compatibility plan — PLAN ONLY" "$text_output" || fail "text plan identifies plan-only mode"
grep -Fq "Official baseline: 947e2fc002d6831c7888b29b5761d59d29e69727" "$text_output" || fail "text plan names the pinned baseline"
grep -Fq "Allowed system changes: none. Ready to apply: no." "$text_output" || fail "text plan reports no allowed system changes"
pass "text plan renders the read-only policy"

expect_rejected "invalid target is rejected" --target nope
expect_rejected "invalid format is rejected" --target rpi5 --format yaml
expect_rejected "--apply is rejected" --target rpi5 --apply
expect_rejected "unknown option is rejected" --target rpi5 --unknown

if [[ -s $sentinel_log ]]; then
  fail "planning invokes no sudo, pacman, systemctl, or ssh" "$(cat "$sentinel_log")"
fi
pass "planning invokes no sudo, pacman, systemctl, or ssh"

# Import the planner directly for deterministic host/model detection checks.
# The fake device-tree path returns a NUL-terminated model exactly as Linux
# exposes it; the Pi 5 match must stop at a model boundary.
if ! PYTHONDONTWRITEBYTECODE=1 python3 - "$planner" <<'PY'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("omarchy_arm64_plan", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

assert module.detect_target("auto", machine="arm64", model="generic board") == {
    "architecture": "aarch64", "profile": "arm64", "source": "host", "model": "generic board"
}
assert module.detect_target("auto", machine="aarch64", model="Raspberry Pi 5 Model B")["profile"] == "rpi5"
assert module.detect_target("auto", machine="aarch64", model="Raspberry Pi 50")["profile"] == "arm64"
try:
    module.detect_target("auto", machine="x86_64", model="Raspberry Pi 5 Model B")
except ValueError:
    pass
else:
    raise AssertionError("x86_64 auto detection was accepted")

class FakePath:
    def __init__(self, filename):
        self.filename = filename

    def read_bytes(self):
        if self.filename == "/sys/firmware/devicetree/base/model":
            return b"Raspberry Pi 5 Model B\0"
        raise FileNotFoundError(self.filename)

module.Path = FakePath
assert module.detect_target("auto", machine="aarch64")["profile"] == "rpi5"
PY
then
  fail "ARM64 host and Raspberry Pi target detection"
fi
pass "ARM64 host and Raspberry Pi target detection"

copy_fixture() {
  local fixture="$1"
  mkdir -p "$fixture/install" "$fixture/bin"
  cp -a "$ROOT/install/arm64" "$fixture/install/"
  cp "$ROOT/install/omarchy-base.packages" "$fixture/install/"
  cp "$wrapper" "$fixture/bin/"
}

fixture_fails() {
  local description="$1"
  local fixture="$2"
  local output="$test_tmp/fixture-output"

  if (cd /tmp && ARM64_PLAN_SENTINEL_LOG="$sentinel_log" OMARCHY_PATH="$fixture" \
      PATH="$stub_bin:$original_path" "$fixture/bin/omarchy-install-plan" \
      --target rpi5 --format json >"$output" 2>&1); then
    fail "$description" "fixture unexpectedly produced a plan"
  fi
  pass "$description"
}

# Adding an official base package without a policy row must stop planning.
drift_fixture="$test_tmp/fixture-drift"
copy_fixture "$drift_fixture"
printf '%s\n' 'arm64-plan-unclassified' >>"$drift_fixture/install/omarchy-base.packages"
fixture_fails "base manifest drift is rejected" "$drift_fixture"

# A duplicate policy row is ambiguous even when its package is classified.
duplicate_fixture="$test_tmp/fixture-duplicate"
copy_fixture "$duplicate_fixture"
first_row=$(awk 'NF && $1 !~ /^#/ { print; exit }' "$duplicate_fixture/install/arm64/packages.tsv")
printf '%s\n' "$first_row" >>"$duplicate_fixture/install/arm64/packages.tsv"
fixture_fails "duplicate policy rows are rejected" "$duplicate_fixture"

# A malformed row must never be silently treated as a package candidate.
malformed_fixture="$test_tmp/fixture-malformed"
copy_fixture "$malformed_fixture"
printf '%s\n' $'arm64-plan-malformed\tcandidate\t-' >>"$malformed_fixture/install/arm64/packages.tsv"
fixture_fails "malformed policy rows are rejected" "$malformed_fixture"

pass "ARM64 plan tests complete"
