#!/bin/bash

set -euo pipefail
source "$(dirname "$0")/base-test.sh"

script="$ROOT/install/arm64/setup-desktop-theme.sh"
bash -n "$script" || fail "desktop theme setup parses as shell"

test_tmp=$(mktemp -d)
trap 'rm -rf "$test_tmp"' EXIT

release="$test_tmp/release"
home="$test_tmp/home"
mock_bin="$test_tmp/bin"
mkdir -p "$release/bin" "$release/config/foot" "$home" "$mock_bin"

cat >"$release/bin/omarchy-theme-set" <<'SH'
#!/bin/bash
set -euo pipefail
state="$HOME/.local/state/omarchy/current"
printf '%s\n' "$*" >>"$OMARCHY_TEST_THEME_CALLS"
mkdir -p "$state/theme/backgrounds"
printf '%s\n' "$1" >"$state/theme.name"
for file in colors.toml shell.toml foot.ini pi.json; do
  printf 'rendered by the existing Omarchy theme renderer\n' >"$state/theme/$file"
done
printf 'unsupported background\n' >"$state/theme/backgrounds/0-winding-road.webp"
if [[ ${ARM64_THEME_TEST_NO_CITYSCAPE:-0} != 1 ]]; then
  printf 'baseline-compatible background\n' >"$state/theme/backgrounds/5-oma-cityscape.jpg"
fi
if [[ ${ARM64_THEME_TEST_NO_BACKGROUND:-0} != 1 ]]; then
  ln -s theme/backgrounds/0-winding-road.webp "$state/background"
fi
SH
chmod +x "$release/bin/omarchy-theme-set"

cat >"$mock_bin/fc-list" <<'SH'
#!/bin/bash
if [[ ${ARM64_THEME_TEST_JETBRAINS:-0} == 1 ]]; then
  printf '%s\n' 'JetBrainsMono Nerd Font'
fi
SH
chmod +x "$mock_bin/fc-list"

cat >"$release/config/foot/foot.ini" <<'EOF'
[main]
include=~/.local/state/omarchy/current/theme/foot.ini
font=JetBrainsMono Nerd Font:size=9
EOF

calls="$test_tmp/theme-calls"
HOME="$home" OMARCHY_PATH="$release" PATH="$mock_bin:$PATH" \
  OMARCHY_TEST_THEME_CALLS="$calls" ARM64_THEME_TEST_JETBRAINS=0 \
  bash "$script" >"$test_tmp/first.log" || fail "empty-home desktop theme setup succeeds"

grep -Fx 'Tokyo Night' "$calls" >/dev/null || fail "empty-home setup renders the default Tokyo Night theme"
grep -Fx 'include=~/.local/state/omarchy/current/theme/foot.ini' \
  "$home/.config/foot/foot.ini" >/dev/null || fail "Foot keeps its generated theme include"
grep -Fx 'font=monospace:size=9' "$home/.config/foot/foot.ini" >/dev/null || \
  fail "Foot falls back to the generic monospace family"
[[ -f $home/.local/state/omarchy/current/theme/colors.toml ]] || \
  fail "the existing theme renderer produced the rendered theme state"
[[ $(readlink -f "$home/.local/state/omarchy/current/background") == \
  "$home/.local/state/omarchy/current/theme/backgrounds/5-oma-cityscape.jpg" ]] || \
  fail "fresh setup keeps the Pi cityscape as its known default"
pass "empty-home setup renders the default theme and preserves the Pi cityscape"

jetbrains_home="$test_tmp/jetbrains-home"
mkdir -p "$jetbrains_home"
HOME="$jetbrains_home" OMARCHY_PATH="$release" PATH="$mock_bin:$PATH" \
  OMARCHY_TEST_THEME_CALLS="$test_tmp/jetbrains-calls" ARM64_THEME_TEST_JETBRAINS=1 \
  bash "$script" >"$test_tmp/jetbrains.log" || fail "font-present desktop theme setup succeeds"
grep -Fx 'font=JetBrainsMono Nerd Font:size=9' \
  "$jetbrains_home/.config/foot/foot.ini" >/dev/null || \
  fail "Foot keeps JetBrainsMono when the font is installed"
pass "Foot font selection follows the installed font set"

preserved_home="$test_tmp/preserved-home"
preserved_state="$preserved_home/.local/state/omarchy/current"
mkdir -p "$preserved_state/theme/backgrounds" "$preserved_home/.config/foot"
printf '%s\n' 'Solitude' >"$preserved_state/theme.name"
for file in colors.toml shell.toml foot.ini pi.json; do
  printf '%s\n' 'existing theme content' >"$preserved_state/theme/$file"
done
printf '%s\n' 'operator-selected background' >"$preserved_state/theme/backgrounds/operator-selected.webp"
ln -s theme/backgrounds/operator-selected.webp "$preserved_state/background"
printf '%s\n' 'operator-owned Foot configuration' >"$preserved_home/.config/foot/foot.ini"
cp "$preserved_home/.config/foot/foot.ini" "$test_tmp/preserved-foot.before"
preserved_background=$(readlink "$preserved_state/background")

HOME="$preserved_home" OMARCHY_PATH="$release" PATH="$mock_bin:$PATH" \
  OMARCHY_TEST_THEME_CALLS="$test_tmp/preserved-calls" ARM64_THEME_TEST_JETBRAINS=0 \
  bash "$script" Tokyo >"$test_tmp/preserved.log" || fail "existing desktop theme setup succeeds"
[[ ! -e $test_tmp/preserved-calls ]] || fail "existing theme setup does not rerender the theme"
cmp -s "$test_tmp/preserved-foot.before" "$preserved_home/.config/foot/foot.ini" || \
  fail "existing Foot configuration is preserved byte-for-byte"
grep -Fx 'Solitude' "$preserved_state/theme.name" >/dev/null || \
  fail "existing theme selection is preserved"
[[ $(readlink "$preserved_state/background") == "$preserved_background" ]] || \
  fail "existing user-selected background is preserved"
pass "existing theme and Foot configuration remain untouched"

webp_home="$test_tmp/webp-home"
mkdir -p "$webp_home"
HOME="$webp_home" OMARCHY_PATH="$release" PATH="$mock_bin:$PATH" \
  OMARCHY_TEST_THEME_CALLS="$test_tmp/webp-calls" ARM64_THEME_TEST_NO_CITYSCAPE=1 \
  bash "$script" >"$test_tmp/webp.log" || fail "WebP-only desktop theme setup succeeds"
[[ $(readlink -f "$webp_home/.local/state/omarchy/current/background") == \
  "$webp_home/.local/state/omarchy/current/theme/backgrounds/0-winding-road.webp" ]] || \
  fail "fresh setup accepts the renderer's WebP background when no cityscape exists"
pass "fresh setup accepts renderer-selected WebP backgrounds"

no_background_home="$test_tmp/no-background-home"
mkdir -p "$no_background_home"
if no_background_output=$(HOME="$no_background_home" OMARCHY_PATH="$release" PATH="$mock_bin:$PATH" \
  OMARCHY_TEST_THEME_CALLS="$test_tmp/no-background-calls" ARM64_THEME_TEST_NO_BACKGROUND=1 \
  bash "$script" 2>&1); then
  fail "themes without a selected background are rejected"
fi
grep -F 'did not select a usable background' <<<"$no_background_output" >/dev/null || \
  fail "missing selected background reports a clear error"
pass "fresh setup fails clearly when the renderer selects no background"

partial_home="$test_tmp/partial-home"
mkdir -p "$partial_home/.local/state/omarchy/current"
printf '%s\n' 'partial state' >"$partial_home/.local/state/omarchy/current/partial"
if HOME="$partial_home" OMARCHY_PATH="$release" PATH="$mock_bin:$PATH" \
  OMARCHY_TEST_THEME_CALLS="$test_tmp/partial-calls" bash "$script" >/dev/null 2>&1; then
  fail "unmarked partial theme state is rejected"
fi
[[ ! -e $test_tmp/partial-calls ]] || fail "partial theme state is rejected before rendering"
pass "unmarked partial theme state cannot be overwritten"
