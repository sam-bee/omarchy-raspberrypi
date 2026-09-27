#!/bin/bash

# Create the installer output before presenting its Foot window.  Exclude the
# separate hypr-rdp output: the compositor must retain one output for the
# local installer desktop even when RDP starts first.
set -euo pipefail

: "${XDG_RUNTIME_DIR:?}"
: "${HYPRLAND_INSTANCE_SIGNATURE:?}"

exec 9>"$XDG_RUNTIME_DIR/omarchy-installer-session-$HYPRLAND_INSTANCE_SIGNATURE.lock"
flock -n 9 || exit 0

installer_monitor_count() {
  local monitors
  monitors=$(hyprctl -j monitors 2>/dev/null) || return 1
  python3 -c '
import json, sys
data = json.load(sys.stdin)
assert isinstance(data, list)
names = [monitor["name"] for monitor in data]
assert all(isinstance(name, str) for name in names)
print(sum(not (name == "hypr-rdp" or name.startswith("hypr-rdp-")) for name in names))
' <<< "$monitors"
}

count=""
for attempt in {1..25}; do
  if count=$(installer_monitor_count); then
    break
  fi
  sleep 0.2
done
if [[ -z $count ]]; then
  echo 'Hyprland monitor query did not become ready' >&2
  exit 1
fi

if (( count == 0 )); then
  hyprctl output create headless omarchy-installer
fi

for attempt in {1..25}; do
  if count=$(installer_monitor_count) && (( count > 0 )); then
    exec /usr/bin/foot --app-id=omarchy-installer --title='Omarchy Installer'
  fi
  sleep 0.2
done

echo 'No usable Hyprland output appeared within five seconds' >&2
exit 1
