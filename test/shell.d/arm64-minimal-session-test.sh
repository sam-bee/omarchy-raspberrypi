#!/bin/bash

set -euo pipefail
source "$(dirname "$0")/base-test.sh"

require_command git
require_command tar
require_command python3
bash -n "$ROOT/install/arm64/stage-user-session.sh"
bash -n "$ROOT/install/arm64/session/90-omarchy-pi"
pass "Pi staging and UWSM environment files parse as shell"

python3 - "$ROOT" <<'PY' || fail "minimal session configuration stays bounded"
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
config = json.loads((root / "install/arm64/session/shell.json").read_text())
assert config["version"] == 1
assert "omarchy.idle" in config["disabledPlugins"]
widgets = [entry["id"] for section in config["bar"]["layout"].values() for entry in section]
assert widgets == ["omarchy.workspaces", "omarchy.clock"]

hypr = (root / "install/arm64/session/hyprland.lua").read_text()
assert hypr.index("omarchy_autostart_minimal = true") < hypr.index('require("default.hypr.omarchy")')
assert hypr.index("omarchy_default_bindings = false") < hypr.index('require("default.hypr.omarchy")')

autostart = (root / "default/hypr/autostart.lua").read_text()
guard = autostart.index("if _G.omarchy_autostart_minimal == true then return end")
assert autostart.index('hl.exec_cmd("omarchy-launch-shell")') < guard
for command in ("omarchy-provision-first-run", "omarchy-powerprofiles-init", "omarchy-hyprland-monitor-watch", "udiskie", "omarchy-hook post-boot"):
    assert autostart.index(command) > guard
PY
pass "minimal session excludes deferred startup and idle features"

test_tmp=$(mktemp -d)
trap 'rm -rf "$test_tmp"' EXIT
test_home="$test_tmp/home"
mkdir -p "$test_home"
HOME="$test_home" "$ROOT/install/arm64/stage-user-session.sh" >"$test_tmp/stage.log" || fail "user session stages in an empty home"

revision=$(git -C "$ROOT" rev-parse HEAD)
release="$test_home/.local/share/omarchy-pi/releases/$revision"
current="$test_home/.local/share/omarchy-pi/current"
[[ -f $release/.omarchy-pi-source-commit ]] || fail "staged release records its source commit"
[[ $(cat "$release/.omarchy-pi-source-commit") == "$revision" ]] || fail "staged release records exact commit"
[[ -L $current && $(readlink "$current") == "releases/$revision" ]] || fail "current points to versioned release"

for mapping in \
  "90-omarchy-pi:.config/uwsm/env.d/90-omarchy-pi" \
  "hyprland.lua:.config/hypr/hyprland.lua" \
  "shell.json:.config/omarchy/shell.json" \
  "xdg-terminals.list:.config/xdg-terminals.list"; do
  source_name=${mapping%%:*}
  target_name=${mapping#*:}
  cmp -s "$release/install/arm64/session/$source_name" "$test_home/$target_name" || fail "staged $source_name matches committed payload"
done
pass "staging creates only the intended user payload and versioned source link"

env_result=$(HOME="$test_home" bash -c 'PATH=/usr/bin:/bin; source "$HOME/.config/uwsm/env.d/90-omarchy-pi"; printf "%s\n%s\n%s\n" "$OMARCHY_PATH" "$PATH" "$TERMINAL"')
expected_path="$test_home/.local/share/omarchy-pi/current"
[[ $env_result == "$expected_path"$'\n'"$expected_path/bin:/usr/bin:/bin"$'\n'"xdg-terminal-exec" ]] || fail "UWSM environment selects the staged release and Foot launcher"
pass "UWSM environment selects the user-owned release"

if HOME="$test_home" "$ROOT/install/arm64/stage-user-session.sh" >"$test_tmp/repeat.log" 2>&1; then
  fail "staging refuses to overwrite existing user files"
fi
[[ -L $current && -f $test_home/.config/hypr/hyprland.lua ]] || fail "refused staging preserves existing payload"
pass "staging refuses a second write without changing the first"
