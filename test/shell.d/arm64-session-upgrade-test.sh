#!/bin/bash

set -euo pipefail
source "$(dirname "$0")/base-test.sh"

require_command git
require_command tar
require_command flock
require_command sha256sum
require_command stat
bash -n "$ROOT/install/arm64/stage-user-session.sh"
bash -n "$ROOT/install/arm64/rollback-user-session.sh"
pass "Pi session stage and rollback commands parse"

if rg -q '/home/|(^|[^0-9])1000([^0-9]|$)' "$ROOT/install/arm64/stage-user-session.sh"; then
  fail "session staging contains no fixed home directory or UID"
fi
pass "session staging uses the invoking account and HOME"

test_tmp=$(mktemp -d)
trap 'rm -rf -- "$test_tmp"' EXIT
fixture="$test_tmp/source tree with spaces"
mkdir -p "$fixture/install/arm64"
cp "$ROOT/install/arm64/stage-user-session.sh" "$fixture/install/arm64/"
cp "$ROOT/install/arm64/rollback-user-session.sh" "$fixture/install/arm64/"
cp -a "$ROOT/install/arm64/session" "$fixture/install/arm64/"
git -C "$fixture" init -q
git -C "$fixture" config user.name "Pi Session Test"
git -C "$fixture" config user.email "pi-session-test@example.invalid"
git -C "$fixture" add install
git -C "$fixture" commit -q -m "session revision one"
revision_one=$(git -C "$fixture" rev-parse HEAD)

home="$test_tmp/home for invoking user"
mkdir -p "$home"
chown "$(id -u):$(id -g)" "$home"
HOME="$home" "$fixture/install/arm64/stage-user-session.sh" >"$test_tmp/install.log" || fail "initial session installs in an unconventional HOME"
release_one="$home/.local/share/omarchy-pi/releases/$revision_one"
current="$home/.local/share/omarchy-pi/current"
previous="$home/.local/share/omarchy-pi/previous"
[[ -L $current && $(readlink "$current") == "releases/$revision_one" ]] || fail "initial install selects its versioned release"
[[ ! -e $previous && ! -L $previous ]] || fail "initial install has no previous release"
for mapping in \
  "90-omarchy-pi:.config/uwsm/env.d/90-omarchy-pi" \
  "hyprland.lua:.config/hypr/hyprland.lua" \
  "shell.json:.config/omarchy/shell.json" \
  "xdg-terminals.list:.config/xdg-terminals.list"; do
  source_name=${mapping%%:*}
  target_name=${mapping#*:}
  cmp -s "$release_one/install/arm64/session/$source_name" "$home/$target_name" || fail "initial install publishes $source_name"
done
pass "initial install publishes a complete versioned session under a nonstandard HOME"

if HOME="$home" "$fixture/install/arm64/stage-user-session.sh" >"$test_tmp/repeat.log" 2>&1; then
  fail "staging the active source revision refuses a no-op update"
fi
[[ $(readlink "$current") == "releases/$revision_one" ]] || fail "same-revision refusal leaves current unchanged"
pass "same-revision staging makes no changes"

shell_config="$home/.config/omarchy/shell.json"
printf '\nuser-owned customization\n' >> "$shell_config"
user_shell_config=$(cat "$shell_config")

printf '\n-- revision two\n' >> "$fixture/install/arm64/session/hyprland.lua"
sed -i 's/"HH:mm"/"HH:mm:ss"/' "$fixture/install/arm64/session/shell.json"
printf 'export OMARCHY_PI_TEST_REVISION=2\n' >> "$fixture/install/arm64/session/90-omarchy-pi"
git -C "$fixture" add install/arm64/session
git -C "$fixture" commit -q -m "session revision two"
revision_two=$(git -C "$fixture" rev-parse HEAD)
[[ -z $(git -C "$fixture" status --porcelain --untracked-files=all) ]] || fail "upgrade fixture source is clean"
HOME="$home" "$fixture/install/arm64/stage-user-session.sh" >"$test_tmp/upgrade.log" || fail "committed session revision upgrades"
release_two="$home/.local/share/omarchy-pi/releases/$revision_two"
[[ $(readlink "$current") == "releases/$revision_two" ]] || fail "upgrade selects the new release"
[[ $(readlink "$previous") == "releases/$revision_one" ]] || fail "upgrade records the immediately previous release"
cmp -s "$release_two/install/arm64/session/hyprland.lua" "$home/.config/hypr/hyprland.lua" || fail "unmodified Hyprland config updates"
cmp -s "$release_two/install/arm64/session/90-omarchy-pi" "$home/.config/uwsm/env.d/90-omarchy-pi" || fail "unmodified UWSM config updates"
cmp -s "$release_two/install/arm64/session/xdg-terminals.list" "$home/.config/xdg-terminals.list" || fail "unmodified terminal config updates"
[[ $(cat "$shell_config") == "$user_shell_config" ]] || fail "user-edited shell config survives upgrade"
pass "upgrade updates only untouched configs and records the prior release"

rm "$home/.config/xdg-terminals.list"
HOME="$home" "$fixture/install/arm64/rollback-user-session.sh" >"$test_tmp/rollback.log" || fail "rollback activates the previous release"
[[ $(readlink "$current") == "releases/$revision_one" ]] || fail "rollback restores the previous current release"
[[ $(readlink "$previous") == "releases/$revision_two" ]] || fail "rollback makes the former current release available again"
cmp -s "$release_one/install/arm64/session/hyprland.lua" "$home/.config/hypr/hyprland.lua" || fail "rollback restores untouched Hyprland config"
cmp -s "$release_one/install/arm64/session/90-omarchy-pi" "$home/.config/uwsm/env.d/90-omarchy-pi" || fail "rollback restores untouched UWSM config"
[[ $(cat "$shell_config") == "$user_shell_config" ]] || fail "user-edited config survives rollback"
[[ ! -e $home/.config/xdg-terminals.list ]] || fail "user-removed config stays missing during rollback"
pass "rollback restores untouched configs while preserving edited and missing configs"

HOME="$home" "$fixture/install/arm64/rollback-user-session.sh" >"$test_tmp/rollback-toggle.log" || fail "a second rollback toggles to the other retained release"
[[ $(readlink "$current") == "releases/$revision_two" && $(readlink "$previous") == "releases/$revision_one" ]] || fail "repeated rollback swaps current and previous"
[[ $(cat "$shell_config") == "$user_shell_config" && ! -e $home/.config/xdg-terminals.list ]] || fail "rollback toggle preserves user config state"
pass "repeated rollback toggles between the two retained releases"

fail_home="$test_tmp/home for failure recovery"
mkdir -p "$fail_home"
chown "$(id -u):$(id -g)" "$fail_home"
HOME="$fail_home" "$fixture/install/arm64/stage-user-session.sh" >"$test_tmp/failure-base.log" || fail "failure fixture installs its base release"
old_revision=$revision_two
old_env=$(cat "$fail_home/.config/uwsm/env.d/90-omarchy-pi")
old_hypr=$(cat "$fail_home/.config/hypr/hyprland.lua")

printf '\n-- revision three\n' >> "$fixture/install/arm64/session/hyprland.lua"
printf 'export OMARCHY_PI_TEST_REVISION=3\n' >> "$fixture/install/arm64/session/90-omarchy-pi"
git -C "$fixture" add install/arm64/session
git -C "$fixture" commit -q -m "session revision three"
revision_three=$(git -C "$fixture" rev-parse HEAD)

stub_bin="$test_tmp/stub-bin"
mkdir -p "$stub_bin"
real_mv=$(command -v mv)
cat > "$stub_bin/mv" <<'SH'
#!/bin/bash
target=${@: -1}
if [[ ${PI_FAIL_ONCE_TARGET:-} == "$target" && ! -e ${PI_FAIL_MARKER:?} ]]; then
  : > "$PI_FAIL_MARKER"
  if [[ ${PI_EDIT_TARGET:-} == "$target" ]]; then
    printf '\nconcurrent user edit\n' >> "$target"
  fi
  exit 79
fi
exec "$PI_REAL_MV" "$@"
SH
chmod +x "$stub_bin/mv"
fail_marker="$test_tmp/mv-failed-once"
if HOME="$fail_home" PATH="$stub_bin:$PATH" PI_REAL_MV="$real_mv" PI_FAIL_ONCE_TARGET="$fail_home/.config/hypr/hyprland.lua" PI_FAIL_MARKER="$fail_marker" \
  "$fixture/install/arm64/stage-user-session.sh" >"$test_tmp/upgrade-failure.log" 2>&1; then
  fail "injected config publication failure aborts the upgrade"
fi
[[ -f $fail_marker ]] || fail "failure injection reaches the second managed config"
[[ $(readlink "$fail_home/.local/share/omarchy-pi/current") == "releases/$old_revision" ]] || fail "failed upgrade keeps current unchanged"
[[ ! -e $fail_home/.local/share/omarchy-pi/previous && ! -L $fail_home/.local/share/omarchy-pi/previous ]] || fail "failed first upgrade keeps previous unchanged"
[[ $(cat "$fail_home/.config/uwsm/env.d/90-omarchy-pi") == "$old_env" ]] || fail "failed upgrade restores a config already replaced"
[[ $(cat "$fail_home/.config/hypr/hyprland.lua") == "$old_hypr" ]] || fail "failed upgrade preserves the config that failed publication"
[[ ! -e $fail_home/.local/share/omarchy-pi/.transaction ]] || fail "successful failure recovery clears its transaction journal"
pass "a failed multi-file upgrade restores published configs and leaves release pointers unchanged"

old_env_before_race=$(cat "$fail_home/.config/uwsm/env.d/90-omarchy-pi")
printf '%s\n\nconcurrent user edit\n' "$old_env_before_race" > "$test_tmp/expected-conflicted-env"
conflict_marker="$test_tmp/mv-conflict-once"
if HOME="$fail_home" PATH="$stub_bin:$PATH" PI_REAL_MV="$real_mv" PI_FAIL_ONCE_TARGET="$fail_home/.config/uwsm/env.d/90-omarchy-pi" PI_FAIL_MARKER="$conflict_marker" PI_EDIT_TARGET="$fail_home/.config/uwsm/env.d/90-omarchy-pi" \
  "$fixture/install/arm64/stage-user-session.sh" >"$test_tmp/recovery-conflict.log" 2>&1; then
  fail "recovery conflict aborts the upgrade"
fi
[[ -f $conflict_marker ]] || fail "recovery conflict injection edits the original config inode"
cmp -s "$test_tmp/expected-conflicted-env" "$fail_home/.config/uwsm/env.d/90-omarchy-pi" || fail "recovery conflict preserves the concurrent user edit"
[[ $(readlink "$fail_home/.local/share/omarchy-pi/current") == "releases/$old_revision" ]] || fail "recovery conflict leaves current unchanged"
[[ -d $fail_home/.local/share/omarchy-pi/.transaction ]] || fail "recovery conflict retains the journal and backup"
pass "recovery retains its journal when the original config changes after preflight"

dirty_home="$test_tmp/dirty source home"
mkdir -p "$dirty_home"
chown "$(id -u):$(id -g)" "$dirty_home"
touch "$fixture/untracked-file"
if HOME="$dirty_home" "$fixture/install/arm64/stage-user-session.sh" >"$test_tmp/dirty.log" 2>&1; then
  fail "dirty source checkout is refused"
fi
[[ ! -e $dirty_home/.local/share/omarchy-pi ]] || fail "dirty source refusal happens before staging writes"
rm "$fixture/untracked-file"
pass "dirty source checkout is refused before touching the target HOME"

uid=$(id -u)
if (( EUID == 0 )); then
  require_command setpriv
  alternate_uid=1001
  while [[ $alternate_uid == 0 || $alternate_uid == "$uid" ]]; do (( alternate_uid += 1 )); done
  alternate_gid=$(getent passwd "$alternate_uid" | cut -d: -f4)
  [[ -n $alternate_gid ]] || alternate_gid=$alternate_uid
  uid_home="$test_tmp/home for uid $alternate_uid"
  mkdir -p "$uid_home"
  chown "$alternate_uid:$alternate_gid" "$uid_home"
  setpriv --reuid "$alternate_uid" --regid "$alternate_gid" --clear-groups env HOME="$uid_home" "$fixture/install/arm64/stage-user-session.sh" >"$test_tmp/uid.log" || fail "session staging supports a non-default target UID"
  [[ $(stat -c '%u' "$uid_home/.local/share/omarchy-pi/current") == "$alternate_uid" ]] || fail "release pointer belongs to the invoking non-default UID"
else
  [[ $uid != "0" ]] || fail "session staging runs as an unprivileged user"
  uid_home="$test_tmp/home for uid $uid"
  mkdir -p "$uid_home"
  chown "$uid:$uid" "$uid_home"
  HOME="$uid_home" "$fixture/install/arm64/stage-user-session.sh" >"$test_tmp/uid.log" || fail "session staging supports the invoking account UID"
  [[ $(stat -c '%u' "$uid_home/.local/share/omarchy-pi/current") == "$uid" ]] || fail "release pointer belongs to the invoking UID"
fi
pass "session release ownership follows the invoking UID rather than a fixed account"
