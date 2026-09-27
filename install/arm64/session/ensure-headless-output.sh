#!/bin/bash

# Keep the normal Omarchy startup path, while giving a disconnected Pi one
# Wayland output for SSH/RDP bring-up. A real display always wins.
set -euo pipefail

local_monitor_count() {
  local monitors
  monitors=$(hyprctl -j monitors 2>/dev/null) || return 1
  python3 -c '
import json
import sys

data = json.load(sys.stdin)
assert isinstance(data, list)
names = [monitor["name"] for monitor in data]
assert all(isinstance(name, str) for name in names)
print(sum(not (name == "hypr-rdp" or name.startswith("hypr-rdp-")) for name in names))
' <<<"$monitors"
}

count=""
for _ in {1..25}; do
  if count=$(local_monitor_count); then
    break
  fi
  sleep 0.2
done

[[ -n $count ]] || {
  echo "Omarchy Pi: Hyprland monitor query did not become ready" >&2
  exit 1
}

if (( count == 0 )); then
  hyprctl output create headless omarchy-pi
fi

for _ in {1..25}; do
  if count=$(local_monitor_count) && (( count > 0 )); then
    exit 0
  fi
  sleep 0.2
done

echo "Omarchy Pi: no display output appeared within five seconds" >&2
exit 1
