#!/bin/bash

# Hyprland may fire hyprland.start before a disconnected Pi has an output.
# Hold one launcher per compositor and wait for a real or headless monitor.
set -euo pipefail

: "${XDG_RUNTIME_DIR:?}"
: "${HYPRLAND_INSTANCE_SIGNATURE:?}"

exec 9>"$XDG_RUNTIME_DIR/omarchy-pi-session-$HYPRLAND_INSTANCE_SIGNATURE.lock"
flock -n 9 || exit 0

monitor_count() {
  local monitors
  monitors=$(hyprctl -j monitors 2>/dev/null) || return 1
  python3 -c 'import json, sys; data = json.load(sys.stdin); assert isinstance(data, list); print(len(data))' <<<"$monitors"
}

count=""
for attempt in {1..25}; do
  if count=$(monitor_count); then break; fi
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
  if count=$(monitor_count) && (( count > 0 )); then
    exec omarchy-launch-shell
  fi
  sleep 0.2
done

echo "No Hyprland output appeared within five seconds" >&2
exit 1
