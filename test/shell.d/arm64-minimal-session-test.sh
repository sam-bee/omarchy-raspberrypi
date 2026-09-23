#!/bin/bash

set -euo pipefail
source "$(dirname "$0")/base-test.sh"
unset XDG_CONFIG_HOME

require_command git
require_command tar
require_command python3
require_command flock
require_command timeout
bash -n "$ROOT/install/arm64/stage-user-session.sh"
bash -n "$ROOT/install/arm64/session/90-omarchy-pi"
bash -n "$ROOT/install/arm64/session/start-shell.sh"
pass "Pi staging and UWSM environment files parse as shell"

python3 - "$ROOT" <<'PY' || fail "minimal session configuration stays bounded"
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
config = json.loads((root / "install/arm64/session/shell.json").read_text())
assert config["version"] == 1
service_ids = set()
for manifest_path in (root / "shell/plugins").rglob("manifest.json"):
    manifest = json.loads(manifest_path.read_text())
    if "service" in manifest["kinds"]:
        service_ids.add(manifest["id"])
assert service_ids <= set(config["disabledPlugins"]), service_ids - set(config["disabledPlugins"])
widgets = [entry["id"] for section in config["bar"]["layout"].values() for entry in section]
assert widgets == ["omarchy.workspaces", "omarchy.clock"]

hypr = (root / "install/arm64/session/hyprland.lua").read_text()
assert hypr.index("omarchy_autostart_minimal = true") < hypr.index('require("default.hypr.omarchy")')
assert hypr.index("omarchy_default_bindings = false") < hypr.index('require("default.hypr.omarchy")')

autostart = (root / "default/hypr/autostart.lua").read_text()
guard = autostart.index("if _G.omarchy_autostart_minimal == true then")
assert autostart.index("/install/arm64/session/start-shell.sh") > guard
assert autostart.index('hl.exec_cmd("omarchy-launch-shell")') > guard
for command in ("omarchy-provision-first-run", "omarchy-powerprofiles-init", "omarchy-hyprland-monitor-watch", "udiskie", "omarchy-hook post-boot"):
    assert autostart.index(command) > guard
PY
pass "minimal session excludes deferred startup and first-party services"

test_tmp=$(mktemp -d)
trap 'rm -rf "$test_tmp"' EXIT
test_checkout="$test_tmp/checkout"
revision=$(git -C "$ROOT" rev-parse HEAD)
git clone -q --shared "$ROOT" "$test_checkout" || fail "clean test checkout is available"
git -C "$test_checkout" checkout -q --detach "$revision" || fail "test checkout selects the draft commit"
[[ -z $(git -C "$test_checkout" status --porcelain) ]] || fail "test checkout is clean"

test_home="$test_tmp/home"
mkdir -p "$test_home"
HOME="$test_home" "$test_checkout/install/arm64/stage-user-session.sh" >"$test_tmp/stage.log" || fail "user session stages in an empty home"

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
pass "staging creates four user config files and the versioned source link"

env_result=$(HOME="$test_home" bash -c 'PATH=/usr/bin:/bin; source "$HOME/.config/uwsm/env.d/90-omarchy-pi"; printf "%s\n%s\n%s\n" "$OMARCHY_PATH" "$PATH" "$TERMINAL"')
expected_path="$test_home/.local/share/omarchy-pi/current"
[[ $env_result == "$expected_path"$'\n'"$expected_path/bin:/usr/bin:/bin"$'\n'"xdg-terminal-exec" ]] || fail "UWSM environment selects the staged release and Foot launcher"
pass "UWSM environment selects the user-owned release"

if HOME="$test_home" "$test_checkout/install/arm64/stage-user-session.sh" >"$test_tmp/repeat.log" 2>&1; then
  fail "staging refuses to overwrite existing user files"
fi
[[ -L $current && -f $test_home/.config/hypr/hyprland.lua ]] || fail "refused staging preserves existing payload"
pass "staging refuses a second write without changing the first"

other_config_home="$test_tmp/other-config-home"
mkdir -p "$other_config_home"
if HOME="$other_config_home" XDG_CONFIG_HOME="$other_config_home/custom-config" "$test_checkout/install/arm64/stage-user-session.sh" >"$test_tmp/xdg.log" 2>&1; then
  fail "staging refuses a nondefault XDG_CONFIG_HOME"
fi
[[ ! -e $other_config_home/.local/share/omarchy-pi ]] || fail "XDG_CONFIG_HOME rejection happens before staging writes"
pass "nondefault XDG_CONFIG_HOME is rejected before writing"

locked_home="$test_tmp/locked-home"
mkdir -p "$locked_home/.local/share/omarchy-pi"
lock_file="$locked_home/.local/share/omarchy-pi/.stage.lock"
exec 9>"$lock_file"
flock -n 9 || fail "test can acquire the staging lock"
if HOME="$locked_home" "$test_checkout/install/arm64/stage-user-session.sh" >"$test_tmp/locked.log" 2>&1; then
  fail "concurrent staging refuses the exclusive lock"
fi
[[ ! -e $locked_home/.local/share/omarchy-pi/current ]] || fail "lock refusal does not stage a release"
flock -u 9
exec 9>&-
pass "exclusive lock refuses a concurrent stage without changing the home"

dirty_home="$test_tmp/dirty-home"
mkdir -p "$dirty_home"
touch "$test_checkout/untracked-pi-test-file"
if HOME="$dirty_home" "$test_checkout/install/arm64/stage-user-session.sh" >"$test_tmp/dirty.log" 2>&1; then
  fail "dirty source checkout is refused"
fi
[[ ! -e $dirty_home/.local/share/omarchy-pi ]] || fail "dirty source refusal happens before staging writes"
rm "$test_checkout/untracked-pi-test-file"
pass "dirty source is refused independently of the clean-checkout functional test"

real_install=$(command -v install)
stub_bin="$test_tmp/stub-bin"
mkdir -p "$stub_bin"
cat >"$stub_bin/install" <<'SH'
#!/bin/bash
target=${@: -1}
if [[ $target == "$HOME/.config/hypr/hyprland.lua" ]]; then
  mkdir -p "$(dirname "$target")"
  printf 'another writer\n' >"$target"
  if [[ ${PI_REPLACE_EARLIER:-0} == 1 ]]; then
    printf 'edited after creation\n' >"$HOME/.config/uwsm/env.d/90-omarchy-pi"
  fi
  exit 77
fi
exec "$PI_REAL_INSTALL" "$@"
SH
chmod +x "$stub_bin/install"

for changed in 0 1; do
  partial_home="$test_tmp/partial-$changed"
  mkdir -p "$partial_home"
  if HOME="$partial_home" PATH="$stub_bin:$PATH" PI_REAL_INSTALL="$real_install" PI_REPLACE_EARLIER="$changed" \
    "$test_checkout/install/arm64/stage-user-session.sh" >"$test_tmp/partial-$changed.log" 2>&1; then
    fail "injected partial stage fails"
  fi
  [[ $(cat "$partial_home/.config/hypr/hyprland.lua") == "another writer" ]] || fail "cleanup preserves a foreign file created during failure"
  env_file="$partial_home/.config/uwsm/env.d/90-omarchy-pi"
  if (( changed == 0 )); then
    [[ ! -e $env_file ]] || fail "cleanup removes its unchanged file"
  else
    [[ $(cat "$env_file") == "edited after creation" ]] || fail "cleanup preserves a modified file"
  fi
  [[ ! -e $partial_home/.local/share/omarchy-pi/current ]] || fail "partial stage has no current release link"
done
pass "partial cleanup removes only unchanged files created by that run"

headless_bin="$test_tmp/headless-bin"
mkdir -p "$headless_bin"
cat >"$headless_bin/hyprctl" <<'SH'
#!/bin/bash
if [[ $* == "-j monitors" ]]; then
  if [[ -f $PI_FAKE_MONITORS ]]; then cat "$PI_FAKE_MONITORS"; else printf '[]\n'; fi
elif [[ $* == "output create headless omarchy-pi" ]]; then
  printf 'create\n' >>"$PI_FAKE_LOG"
  if [[ ${PI_FAKE_NO_OUTPUT:-0} != 1 ]]; then printf '[{"name":"HEADLESS-0"}]\n' >"$PI_FAKE_MONITORS"; fi
else
  exit 88
fi
SH
cat >"$headless_bin/omarchy-launch-shell" <<'SH'
#!/bin/bash
printf 'launch\n' >>"$PI_FAKE_LOG"
sleep "${PI_FAKE_LAUNCH_SLEEP:-0}"
SH
chmod +x "$headless_bin/hyprctl" "$headless_bin/omarchy-launch-shell"

runtime_dir="$test_tmp/runtime"
mkdir -p "$runtime_dir"
monitor_file="$test_tmp/monitors.json"
headless_log="$test_tmp/headless.log"
helper="$test_checkout/install/arm64/session/start-shell.sh"
XDG_RUNTIME_DIR="$runtime_dir" HYPRLAND_INSTANCE_SIGNATURE=one PI_FAKE_MONITORS="$monitor_file" PI_FAKE_LOG="$headless_log" \
  PI_FAKE_LAUNCH_SLEEP=1 PATH="$headless_bin:$PATH" bash "$helper" &
first_pid=$!
for attempt in {1..40}; do
  [[ -f $headless_log ]] && break
  sleep 0.05
done
[[ -f $headless_log ]] || fail "headless helper reached compositor output creation"
XDG_RUNTIME_DIR="$runtime_dir" HYPRLAND_INSTANCE_SIGNATURE=one PI_FAKE_MONITORS="$monitor_file" PI_FAKE_LOG="$headless_log" \
  PATH="$headless_bin:$PATH" bash "$helper" || fail "duplicate helper invocation exits cleanly"
wait "$first_pid" || fail "first helper invocation completes"
[[ $(rg -c '^create$' "$headless_log") == 1 && $(rg -c '^launch$' "$headless_log") == 1 ]] || fail "headless helper creates one output and launches one shell"
pass "headless helper creates and waits for one output before one shell launch"

printf '[{"name":"HDMI-A-1"}]\n' >"$monitor_file"
: >"$headless_log"
XDG_RUNTIME_DIR="$runtime_dir" HYPRLAND_INSTANCE_SIGNATURE=two PI_FAKE_MONITORS="$monitor_file" PI_FAKE_LOG="$headless_log" \
  PATH="$headless_bin:$PATH" bash "$helper" || fail "helper starts with an existing output"
[[ $(cat "$headless_log") == "launch" ]] || fail "existing output skips headless creation"
pass "existing physical output skips headless creation"

rm -f "$monitor_file"
: >"$headless_log"
if XDG_RUNTIME_DIR="$runtime_dir" HYPRLAND_INSTANCE_SIGNATURE=three PI_FAKE_MONITORS="$monitor_file" PI_FAKE_LOG="$headless_log" \
  PI_FAKE_NO_OUTPUT=1 PATH="$headless_bin:$PATH" timeout 7s bash "$helper" >"$test_tmp/no-output.log" 2>&1; then
  fail "helper does not launch a shell without an output"
fi
[[ $(cat "$headless_log") == "create" ]] || fail "missing output attempts only one headless creation"
[[ -s $test_tmp/no-output.log ]] || fail "missing output reports a bounded failure"
pass "missing output fails within a bounded wait"
