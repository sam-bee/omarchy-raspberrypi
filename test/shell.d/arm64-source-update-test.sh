#!/bin/bash

set -euo pipefail
source "$(dirname "$0")/base-test.sh"

require_command git
require_command python3

helper="$ROOT/install/arm64/update-source.py"
python3 -m py_compile "$helper" || fail "Pi source updater parses as Python"
pass "Pi source updater parses as Python"

test_tmp=$(mktemp -d)
trap 'rm -rf -- "$test_tmp"' EXIT

seed="$test_tmp/seed"
remote="$test_tmp/remote.git"
mkdir -p "$seed/install/arm64"
cp "$ROOT/install/arm64/stage-user-session.sh" "$seed/install/arm64/"
cp "$ROOT/install/arm64/rollback-user-session.sh" "$seed/install/arm64/"
cp "$ROOT/install/arm64/update-source.py" "$seed/install/arm64/"
cp -a "$ROOT/install/arm64/session" "$seed/install/arm64/"
git -C "$seed" init -q
git -C "$seed" config user.name "Pi Source Update Test"
git -C "$seed" config user.email "pi-source-update-test@example.invalid"
git -C "$seed" add install
git -C "$seed" commit -q -m "source revision one"
revision_one=$(git -C "$seed" rev-parse HEAD)
git init --bare -q "$remote"
git -C "$seed" remote add origin "$remote"
git -C "$seed" push -q origin "HEAD:refs/heads/quattro-rpi5"

git_config="$test_tmp/gitconfig"
git config --file "$git_config" url."$remote".insteadOf "https://github.com/sam-bee/omarchy-raspberrypi.git"
task_home="$test_tmp/home"
mkdir -p "$task_home"
chown "$(id -u):$(id -g)" "$task_home"

run_helper() {
  HOME="$task_home" GIT_CONFIG_GLOBAL="$git_config" "$helper" "$@"
}

prepare_output=$(run_helper prepare --json)
python3 - "$prepare_output" "$revision_one" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
assert payload["status"] == "prepared", payload
assert payload["revision"] == sys.argv[2], payload
assert payload["checkout"].endswith("/" + sys.argv[2]), payload
PY
[[ ! -e $task_home/.local/share/omarchy-pi/releases ]] || fail "prepare does not publish a final release"
pass "prepare resolves the branch and creates a clean private checkout"

run_helper activate "$revision_one" >"$test_tmp/activate-one.log"
current="$task_home/.local/share/omarchy-pi/current"
previous="$task_home/.local/share/omarchy-pi/previous"
release_one="$task_home/.local/share/omarchy-pi/releases/$revision_one"
[[ -L $current && $(readlink "$current") == "releases/$revision_one" ]] || fail "activate publishes the prepared release"
[[ -f $release_one/.omarchy-pi-source-commit && $(cat "$release_one/.omarchy-pi-source-commit") == "$revision_one" ]] || fail "stager writes the exact release marker"
[[ ! -e $previous && ! -L $previous ]] || fail "first source activation has no previous release"
pass "activate delegates final publication to the clean checkout stager"

check_output=$(run_helper check)
[[ $check_output == "up-to-date $revision_one" ]] || fail "check reports a successful no-change result" "$check_output"
run_helper activate "$revision_one" >"$test_tmp/no-change.log"
grep -Fx "already-active $revision_one" "$test_tmp/no-change.log" >/dev/null || fail "activate treats an active revision as a successful no-op"
pass "no-change source checks and activation succeed"

bare_cache="$task_home/.cache/omarchy-pi/source.git"
git --git-dir "$bare_cache" remote set-url origin "$test_tmp/wrong-source.git"
set +e
run_helper prepare --revision "$revision_one" >"$test_tmp/wrong-origin.log" 2>&1
wrong_origin_status=$?
set -e
[[ $wrong_origin_status -eq 2 ]] || fail "prepare rejects a private cache with the wrong origin" "status=$wrong_origin_status"
git --git-dir "$bare_cache" remote set-url origin "https://github.com/sam-bee/omarchy-raspberrypi.git"
pass "prepare validates the fixed HTTPS source origin"

printf '\nrevision two\n' >> "$seed/install/arm64/session/hyprland.lua"
git -C "$seed" add install/arm64/session/hyprland.lua
git -C "$seed" commit -q -m "source revision two"
revision_two=$(git -C "$seed" rev-parse HEAD)
git -C "$seed" push -q origin "HEAD:refs/heads/quattro-rpi5"
check_output=$(run_helper check)
[[ $check_output == "update-available $revision_two (current $revision_one)" ]] || fail "check detects a remote source revision" "$check_output"
prepare_output=$(run_helper prepare --json)
python3 - "$prepare_output" "$revision_two" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
assert payload["status"] == "prepared", payload
assert payload["revision"] == sys.argv[2], payload
PY
run_helper activate "$revision_two" --json >"$test_tmp/activate-two.json"
python3 - "$test_tmp/activate-two.json" "$revision_two" <<'PY'
import json
import sys

payload = json.loads(open(sys.argv[1], encoding="utf-8").read())
assert payload == {"revision": sys.argv[2], "status": "activated"}, payload
PY
[[ $(readlink "$current") == "releases/$revision_two" ]] || fail "a new source activation selects its release"
[[ $(readlink "$previous") == "releases/$revision_one" ]] || fail "a new source activation retains the previous release"
pass "prepare and activate remain separate across source revisions"

bad_config="$test_tmp/offline-gitconfig"
git config --file "$bad_config" url."$test_tmp/no-such-remote".insteadOf "https://github.com/sam-bee/omarchy-raspberrypi.git"
set +e
HOME="$task_home" GIT_CONFIG_GLOBAL="$bad_config" "$helper" check >"$test_tmp/offline.log" 2>&1
offline_status=$?
set -e
if (( offline_status == 0 )); then
  fail "offline source check fails instead of claiming up-to-date"
fi
[[ $offline_status -eq 2 ]] || fail "offline source check returns an unavailable status" "status=$offline_status"
! grep -q '^up-to-date ' "$test_tmp/offline.log" || fail "offline source check does not report up-to-date"
pass "offline source resolution is an explicit failure"

run_helper rollback --json >"$test_tmp/rollback.json"
python3 - "$test_tmp/rollback.json" "$revision_one" <<'PY'
import json
import sys

payload = json.loads(open(sys.argv[1], encoding="utf-8").read())
assert payload == {"current_revision": sys.argv[2], "status": "rolled-back"}, payload
PY
[[ $(readlink "$current") == "releases/$revision_one" ]] || fail "rollback returns to the previous source release"
[[ $(readlink "$previous") == "releases/$revision_two" ]] || fail "rollback retains the former source release"
status_output=$(run_helper status)
[[ $status_output == "current $revision_one previous $revision_two" ]] || fail "status reports current and previous source releases" "$status_output"
pass "source rollback and current/previous status use the existing stager pointers"
