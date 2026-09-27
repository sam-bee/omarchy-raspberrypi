#!/bin/bash

set -euo pipefail
source "$(dirname "$0")/base-test.sh"

script="$ROOT/install/arm64/setup-desktop-images.sh"
bash -n "$script" || fail "desktop image setup parses as shell"

test_tmp=$(mktemp -d)
trap 'rm -rf "$test_tmp"' EXIT

home="$test_tmp/home"
mock_bin="$test_tmp/bin"
mkdir -p "$home/.config" "$home/.local/state/omarchy/current" "$mock_bin"

cat >"$mock_bin/imv" <<'SH'
#!/bin/bash
exit 0
SH
chmod +x "$mock_bin/imv"

printf '%s\n' 'Tokyo Night' >"$home/.local/state/omarchy/current/theme.name"
printf '%s\n' 'current Pi cityscape' >"$home/current-cityscape.jpg"
ln -s "$home/current-cityscape.jpg" "$home/.local/state/omarchy/current/background"

cat >"$home/.config/mimeapps.list" <<'EOF'
[Default Applications]
inode/directory=org.gnome.Nautilus.desktop
x-scheme-handler/mailto=custom-mail.desktop
image/png=old-viewer.desktop

[Added Associations]
inode/directory=org.gnome.Nautilus.desktop;
EOF

HOME="$home" XDG_CONFIG_HOME="$home/.config" XDG_CURRENT_DESKTOP=X-Generic \
  XDG_UTILS_INSTALL_MODE=user OMARCHY_PATH="$ROOT" PATH="$mock_bin:/usr/bin:/bin" \
  bash "$script" >"$test_tmp/first.log" || fail "empty-home image setup succeeds"

grep -Fx 'inode/directory=org.gnome.Nautilus.desktop' "$home/.config/mimeapps.list" >/dev/null || \
  fail "image setup preserves the file-manager MIME default"
grep -Fx 'x-scheme-handler/mailto=custom-mail.desktop' "$home/.config/mimeapps.list" >/dev/null || \
  fail "image setup preserves the mail MIME default"
grep -Fx 'image/png=imv.desktop' "$home/.config/mimeapps.list" >/dev/null || \
  fail "image setup assigns PNG files to imv"
grep -Fx 'image/jpg=imv.desktop' "$home/.config/mimeapps.list" >/dev/null || \
  fail "image setup assigns JPG files to imv"
grep -Fx 'image/webp=imv.desktop' "$home/.config/mimeapps.list" >/dev/null || \
  fail "image setup assigns WebP files to imv"
[[ -f $home/.config/imv/config ]] || fail "image setup creates the user imv config"
grep -F '<Ctrl+b> = exec omarchy-theme-bg-set "$imv_current_file" &' \
  "$home/.config/imv/config" >/dev/null || fail "imv config offers a background action"
[[ -d "$home/.config/omarchy/backgrounds/Tokyo Night" ]] || \
  fail "image setup creates the active theme's user background directory"
[[ $(readlink "$home/.local/state/omarchy/current/background") == "$home/current-cityscape.jpg" ]] || \
  fail "image setup preserves the selected cityscape"
pass "image setup adds user image handling without changing unrelated defaults"

printf '%s\n' 'user-managed imv configuration' >"$home/.config/imv/config"
cp "$home/.config/imv/config" "$test_tmp/imv-before"
HOME="$home" XDG_CONFIG_HOME="$home/.config" XDG_CURRENT_DESKTOP=X-Generic \
  XDG_UTILS_INSTALL_MODE=user OMARCHY_PATH="$ROOT" PATH="$mock_bin:/usr/bin:/bin" \
  bash "$script" >"$test_tmp/second.log" || fail "repeat image setup succeeds"
cmp -s "$test_tmp/imv-before" "$home/.config/imv/config" || \
  fail "repeat image setup preserves a user-managed imv config"
[[ $(readlink "$home/.local/state/omarchy/current/background") == "$home/current-cityscape.jpg" ]] || \
  fail "repeat image setup preserves the selected cityscape"
pass "repeat image setup is idempotent for user config and wallpaper"

alternate_config="$test_tmp/alternate-config"
if HOME="$home" XDG_CONFIG_HOME="$alternate_config" XDG_CURRENT_DESKTOP=X-Generic \
  XDG_UTILS_INSTALL_MODE=user OMARCHY_PATH="$ROOT" PATH="$mock_bin:/usr/bin:/bin" \
  bash "$script" >"$test_tmp/rejected.log" 2>&1; then
  fail "image setup rejects a non-default XDG_CONFIG_HOME"
fi
grep -F 'requires XDG_CONFIG_HOME to be unset or' "$test_tmp/rejected.log" >/dev/null || \
  fail "image setup explains the XDG_CONFIG_HOME restriction"
[[ ! -e $alternate_config ]] || fail "rejected XDG_CONFIG_HOME receives no image setup files"
pass "image setup rejects an alternate XDG_CONFIG_HOME"
