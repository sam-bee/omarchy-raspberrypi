#!/bin/bash

set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

background_qml="$ROOT/shell/plugins/background/Background.qml"
pi_hypr="$ROOT/install/arm64/session/hyprland.lua"
menu_images="$ROOT/bin/omarchy-menu-images"
bg_switcher="$ROOT/bin/omarchy-theme-bg-switcher"
bg_cache="$ROOT/bin/omarchy-theme-bg-cache"

bash -n "$menu_images" "$bg_switcher" "$bg_cache"

grep -qF -- '--still-only' "$menu_images" ||
  fail "image selector supports a still-image-only mode"
grep -qF 'menu_args+=(--still-only)' "$bg_switcher" ||
  fail "Pi background switcher requests still images only"
grep -qF 'menu_args+=(--still-only)' "$bg_cache" ||
  fail "Pi background cache warmer requests still images only"

still_test_tmp=$(mktemp -d)
trap 'rm -rf "$still_test_tmp"' EXIT
mkdir -p "$still_test_tmp/images" "$still_test_tmp/bin"
printf 'still' >"$still_test_tmp/images/still.png"
printf 'video' >"$still_test_tmp/images/movie.mp4"
cat >"$still_test_tmp/bin/vipsthumbnail" <<'SH'
#!/bin/bash
while (( $# > 0 )); do
  if [[ $1 == "--path" ]]; then
    output=${2%%\[*}
    printf 'thumbnail' >"$output"
    exit 0
  fi
  shift
done
exit 1
SH
chmod +x "$still_test_tmp/bin/vipsthumbnail"
cat >"$still_test_tmp/bin/ffmpegthumbnailer" <<'SH'
#!/bin/bash
while (( $# > 0 )); do
  if [[ $1 == "-o" ]]; then
    printf 'thumbnail' >"$2"
    exit 0
  fi
  shift
done
exit 1
SH
chmod +x "$still_test_tmp/bin/ffmpegthumbnailer"

mkdir -p "$still_test_tmp/home/.local/state/omarchy/current/theme/backgrounds"
printf '%s\n' 'Tokyo Night' >"$still_test_tmp/home/.local/state/omarchy/current/theme.name"
cat >"$still_test_tmp/bin/omarchy-menu-images" <<'SH'
#!/bin/bash
printf '%s\n' "$*" >"$PI_IMAGE_MENU_ARGS"
SH
chmod +x "$still_test_tmp/bin/omarchy-menu-images"
HOME="$still_test_tmp/home" PATH="$still_test_tmp/bin:$PATH" \
  PI_IMAGE_MENU_ARGS="$still_test_tmp/menu-args" OMARCHY_PI_MINIMAL_SESSION=1 \
  "$bg_cache"
grep -qF -- '--still-only --cache-only' "$still_test_tmp/menu-args" ||
  fail "Pi background cache warmer passes still-only mode"

HOME="$still_test_tmp/home" PATH="$still_test_tmp/bin:$PATH" \
  PI_IMAGE_MENU_ARGS="$still_test_tmp/menu-args-normal" \
  "$bg_cache"
if grep -qF -- '--still-only' "$still_test_tmp/menu-args-normal"; then
  fail "non-Pi background cache warmer does not add still-only mode"
fi
grep -qF -- '--cache-only' "$still_test_tmp/menu-args-normal" ||
  fail "non-Pi background cache warmer keeps cache-only mode"
pass "Pi cache warmup avoids unsupported video thumbnail generation"

XDG_CACHE_HOME="$still_test_tmp/cache" PATH="$still_test_tmp/bin:$PATH" \
  "$menu_images" --cache-only "$still_test_tmp/images"
rows_file=$(find "$still_test_tmp/cache/omarchy/image-selector" -name '*.rows' -type f -print -quit)
[[ -n $rows_file ]] || fail "still-only image selector publishes rows"
grep -qF 'movie.mp4' "$rows_file" || fail "generic image selector keeps video files"

XDG_CACHE_HOME="$still_test_tmp/cache" PATH="$still_test_tmp/bin:$PATH" \
  "$menu_images" --cache-only --still-only "$still_test_tmp/images"
rows_file=$(find "$still_test_tmp/cache/omarchy/image-selector" -name '*.rows' -type f -print -quit)
[[ -n $rows_file ]] || fail "still-only image selector publishes rows"
grep -qF 'still.png' "$rows_file" || fail "still-only image selector keeps still images"
if grep -qF 'movie.mp4' "$rows_file"; then
  fail "still-only image selector hides video files"
fi
pass "Pi background picker hides unsupported video backgrounds"

grep -qF 'if (!bgSwitchProc.running) bgSwitchProc.running = true' "$background_qml" ||
  fail "Pi background service keeps the still-background selector available"

python3 - "$background_qml" <<'PY'
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_text()
selector = source.index("function openSelector()")
theme = source.index("function openThemeSwitcher()")
selector_body = source[selector:theme]
assert "if (piMinimalSession) return" not in selector_body, "minimal mode must allow background selection"
assert "if (piMinimalSession) return" in source[theme:source.index("Process {", theme)], "minimal mode must keep full theme switching gated"
assert "if (!root.piMinimalSession) root.openThemeSwitcher()" in source, "right desktop click must keep theme switching gated"
assert "root.openSelector()" in source, "left desktop double-click must open background selection"
PY
pass "Pi background selection stays available while full theme switching remains gated"

grep -qF 'o.bind("SUPER + CTRL + SPACE", "Background switcher", "omarchy-menu toggle background")' "$pi_hypr" ||
  fail "Pi profile keeps the upstream background shortcut"
grep -qF 'o.bind("SUPER + SHIFT + F", "File manager", { omarchy = "nautilus" })' "$pi_hypr" ||
  fail "Pi profile exposes the upstream file-manager shortcut"

if grep -qF 'omarchy-theme-switcher' "$pi_hypr"; then
  fail "Pi profile does not expose the unavailable full theme switcher"
fi
pass "Pi profile exposes discoverable background and image-file workflows only"
