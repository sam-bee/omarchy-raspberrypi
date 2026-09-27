#!/bin/bash

set -euo pipefail
source "$(dirname "$0")/base-test.sh"

hypr="$ROOT/install/arm64/session/hyprland.lua"
for binding in \
  'o.bind("XF86AudioRaiseVolume", "Volume up", "omarchy-audio-output-volume raise"' \
  'o.bind("XF86AudioLowerVolume", "Volume down", "omarchy-audio-output-volume lower"' \
  'o.bind("XF86AudioMute", "Mute", "omarchy-audio-output-volume mute-toggle"' \
  'o.bind("ALT + XF86AudioRaiseVolume", "Volume up precise", "omarchy-audio-output-volume +1"' \
  'o.bind("ALT + XF86AudioLowerVolume", "Volume down precise", "omarchy-audio-output-volume -1"' \
  'o.bind("SUPER + CTRL + A", "Audio", "omarchy-shell shell toggle omarchy.audio"'; do
  grep -Fq "$binding" "$hypr" || fail "Pi Hyprland profile exposes $binding"
done
! grep -Fq 'XF86AudioMicMute' "$hypr" || fail "Pi profile does not advertise an unverified microphone device"
grep -Fq 'hl.bind("SUPER + B", hl.dsp.exec_cmd("omarchy-launch-browser")' "$hypr" ||
  fail "audio bindings preserve the browser shortcut"
pass "Pi profile exposes bounded audio bindings and preserves browser shortcuts"

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

audio_bin="$tmpdir/audio-bin"
mkdir -p "$audio_bin" "$tmpdir/runtime"
cat >"$audio_bin/pactl" <<'EOF'
#!/bin/bash
case "$1" in
  get-default-sink) printf '%s\n' alsa_output.pi.speaker ;;
  get-sink-volume)
    volume=$(cat "$PACTL_VOLUME_FILE" 2>/dev/null || printf '%s' 50)
    printf 'Volume: front-left: 32768 / %s%% / 0.00 dB, front-right: 32768 / %s%% / 0.00 dB\n' "$volume" "$volume"
    ;;
  get-sink-mute) printf '%s\n' 'Mute: no' ;;
  set-sink-mute) printf '%s\n' "$*" >>"$PACTL_LOG" ;;
  set-sink-volume)
    printf '%s\n' "$*" >>"$PACTL_LOG"
    printf '%s\n' "${3%%%}" >"$PACTL_VOLUME_FILE"
    ;;
  *) printf 'unexpected pactl call: %s\n' "$*" >&2; exit 1 ;;
esac
EOF
cat >"$audio_bin/omarchy-osd" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >"$AUDIO_OSD_CAPTURE"
EOF
chmod +x "$audio_bin/pactl" "$audio_bin/omarchy-osd"

PACTL_LOG="$tmpdir/pactl.log" PACTL_VOLUME_FILE="$tmpdir/pactl-volume" AUDIO_OSD_CAPTURE="$tmpdir/audio-osd" XDG_RUNTIME_DIR="$tmpdir/runtime" \
  PATH="$audio_bin:$ROOT/bin:/usr/bin:/bin" \
  "$ROOT/bin/omarchy-audio-output-volume" raise
grep -Fqx 'set-sink-mute alsa_output.pi.speaker 0' "$tmpdir/pactl.log" ||
  fail "volume raise unmutes the selected PipeWire-Pulse sink"
grep -Fqx 'set-sink-volume alsa_output.pi.speaker 55%' "$tmpdir/pactl.log" ||
  fail "volume raise changes the selected sink by five percent"
grep -Fqx -- '-i volume-high -p 55' "$tmpdir/audio-osd" ||
  fail "volume raise reports the new level through the OSD"
pass "volume helper drives the installed pactl stack and OSD"

osd_bin="$tmpdir/osd-bin"
mkdir -p "$osd_bin"
cat >"$osd_bin/omarchy-shell" <<'EOF'
#!/bin/bash
printf '%s\n' "${4:-}" >"$OSD_PAYLOAD"
EOF
chmod +x "$osd_bin/omarchy-shell"
OSD_PAYLOAD="$tmpdir/osd-payload" PATH="$osd_bin:$ROOT/bin:/usr/bin:/bin" \
  "$ROOT/bin/omarchy-osd" -i $'volume-"\\' -m $'line\tbreak\nnext' -p 75 -d 800
OSD_PAYLOAD=$(cat "$tmpdir/osd-payload") python3 - <<'PY'
import json
import os

payload = json.loads(os.environ["OSD_PAYLOAD"])
assert payload["icon"] == 'volume-"\\'
assert payload["message"] == "line\tbreak\nnext"
assert payload["value"] == "75"
assert payload["progressText"] == "75%"
assert payload["duration"] == "800"
PY
pass "OSD payload remains valid without jq"
