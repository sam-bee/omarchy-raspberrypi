#!/bin/bash

set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

test_tmp=$(mktemp -d)
trap 'rm -rf "$test_tmp"' EXIT

stub_bin="$test_tmp/bin"
output_dir="$test_tmp/pictures"
mkdir -p "$stub_bin"

# Exercise the fallback even on workstations with Omasnap installed. Never
# launch a real screenshot UI from this fixture.
cat >"$stub_bin/omarchy-cmd-present" <<'SH'
#!/bin/bash
exit 1
SH

cat >"$stub_bin/hyprctl" <<'SH'
#!/bin/bash
if [[ $1 == getoption ]]; then
  printf '{"int":1}\n'
fi
SH

cat >"$stub_bin/jq" <<'SH'
#!/bin/bash
printf '1\n'
SH

cat >"$stub_bin/omarchy-notification-send" <<'SH'
#!/bin/bash
printf '%s\n' "$*" >>"$PI_CAPTURE_NOTIFY_LOG"
SH

cat >"$stub_bin/omarchy-capture-region" <<'SH'
#!/bin/bash
if [[ ${PI_CAPTURE_CANCEL:-false} == true ]]; then
  printf '\n'
else
  printf '\n10,20 300x200\n'
fi
SH

cat >"$stub_bin/grim" <<'SH'
#!/bin/bash
if [[ ${PI_CAPTURE_GRIM_FAIL:-false} == true ]]; then
  exit 1
fi
if [[ ${3:-} == - ]]; then
  printf 'PNG from %s\n' "${2:-}"
else
  printf 'PNG from %s\n' "${2:-}" >"${3:-}"
fi
SH

cat >"$stub_bin/wl-copy" <<'SH'
#!/bin/bash
if [[ ${PI_CAPTURE_WL_COPY_FAIL:-false} == true ]]; then
  exit 1
fi
cat >"$PI_CAPTURE_CLIPBOARD"
SH

chmod +x "$stub_bin"/*

run_capture() {
  PI_CAPTURE_NOTIFY_LOG="$test_tmp/notifications" \
  PI_CAPTURE_CLIPBOARD="$test_tmp/clipboard" \
  OMASNAP_SCREENSHOT_DIR="${PI_CAPTURE_OUTPUT_DIR:-$output_dir}" \
  OMARCHY_SCREENSHOT_DIR="${PI_CAPTURE_LEGACY_OUTPUT_DIR:-}" \
  HOME="$test_tmp/home" \
  PATH="$stub_bin:$ROOT/bin:/usr/bin:/bin" \
    "$ROOT/bin/omarchy-capture-screenshot" "$@"
}

mkdir -p "$test_tmp/home"

saved=$(run_capture fullscreen save)
[[ $saved == "$output_dir"/screenshot-*.png ]] || fail "fallback saves screenshots in the configured directory"
[[ -s $saved && $(<"$saved") == "PNG from 10,20 300x200" ]] || fail "fallback passes the selected geometry to grim"
pass "fallback captures and saves a full-screen selection"

rm -f "$test_tmp/clipboard"
PI_CAPTURE_OUTPUT_DIR="$test_tmp/copy-only" run_capture region copy >/dev/null
[[ $(<"$test_tmp/clipboard") == "PNG from 10,20 300x200" ]] || fail "fallback copies region captures as PNG clipboard data"
[[ ! -d "$test_tmp/copy-only" ]] || fail "fallback does not create a screenshots directory for copy-only captures"
pass "fallback copies a region capture to the clipboard"

rm -f "$test_tmp/clipboard"
PI_CAPTURE_CANCEL=true run_capture region copy >/dev/null
[[ ! -e "$test_tmp/clipboard" ]] || fail "fallback leaves the clipboard untouched when selection is cancelled"
pass "fallback handles cancelled selections"

native_dir="$test_tmp/native path"
legacy_dir="$test_tmp/legacy path"
PI_CAPTURE_OUTPUT_DIR="$native_dir" PI_CAPTURE_LEGACY_OUTPUT_DIR="$legacy_dir" \
  native_saved=$(run_capture fullscreen save)
[[ $native_saved == "$native_dir"/screenshot-*.png && ! -e $legacy_dir ]] ||
  fail "fallback gives the native screenshot directory precedence over the legacy variable"
pass "fallback preserves native screenshot directory precedence"

PI_CAPTURE_OUTPUT_DIR="$test_tmp/grim-failure" PI_CAPTURE_GRIM_FAIL=true \
  run_capture fullscreen save >/dev/null 2>&1 && fail "fallback reports grim failures"
[[ -z $(find "$test_tmp/grim-failure" -maxdepth 1 -type f -name '*.png' -print -quit) ]] ||
  fail "fallback removes an incomplete file after grim fails"
pass "fallback propagates grim failures"

PI_CAPTURE_OUTPUT_DIR="$test_tmp/wl-copy-failure" PI_CAPTURE_WL_COPY_FAIL=true \
  run_capture fullscreen slurp >/dev/null 2>&1 && fail "fallback reports wl-copy failures"
[[ -n $(find "$test_tmp/wl-copy-failure" -maxdepth 1 -type f -name '*.png' -print -quit) ]] ||
  fail "fallback keeps the saved screenshot when wl-copy fails"
pass "fallback propagates clipboard failures"

pass "Pi screenshot fallback works without Omasnap or Hyprpicker"
