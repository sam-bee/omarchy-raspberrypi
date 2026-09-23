#!/bin/bash

# Hyprland may fire hyprland.start before a disconnected Pi has an output.
# Hold one launcher per compositor and wait for a real or headless monitor.
set -euo pipefail

: "${XDG_RUNTIME_DIR:?}"
: "${HYPRLAND_INSTANCE_SIGNATURE:?}"

exec 9>"$XDG_RUNTIME_DIR/omarchy-pi-session-$HYPRLAND_INSTANCE_SIGNATURE.lock"
flock -n 9 || exit 0

local_monitor_count() {
  local monitors
  monitors=$(hyprctl -j monitors 2>/dev/null) || return 1
  python3 -c '
import json, sys
data = json.load(sys.stdin)
assert isinstance(data, list)
names = [monitor["name"] for monitor in data]
assert all(isinstance(name, str) for name in names)
# hypr-rdp can create its own output before this launcher runs. It must not
# suppress the fallback output needed for a disconnected Pi desktop.
print(sum(not (name == "hypr-rdp" or name.startswith("hypr-rdp-")) for name in names))
' <<<"$monitors"
}

count=""
for attempt in {1..25}; do
  if count=$(local_monitor_count); then break; fi
  sleep 0.2
done
if [[ -z $count ]]; then
  echo "Hyprland monitor query did not become ready" >&2
  exit 1
fi

if (( count == 0 )); then
  hyprctl output create headless omarchy-pi
fi

for attempt in {1..25}; do
  if count=$(local_monitor_count) && (( count > 0 )); then
    exec omarchy-launch-shell
  fi
  sleep 0.2
done

echo "No Hyprland output appeared within five seconds" >&2
exit 1
