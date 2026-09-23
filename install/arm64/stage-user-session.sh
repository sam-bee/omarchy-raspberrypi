#!/bin/bash

# Stage only a user's minimal graphical session. Package and service changes
# belong to separately reviewed Pi transactions.
set -euo pipefail

if (( $# != 0 || EUID == 0 )); then
  echo "Run as the target user without arguments or sudo" >&2
  exit 2
fi

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
revision=$(git -C "$source_dir" rev-parse --verify HEAD)
if [[ -n $(git -C "$source_dir" status --porcelain --untracked-files=all) ]]; then
  echo "Commit the source tree before staging a versioned session" >&2
  exit 1
fi

data_dir="$HOME/.local/share/omarchy-pi"
release_dir="$data_dir/releases/$revision"
current_link="$data_dir/current"
env_file="$HOME/.config/uwsm/env.d/90-omarchy-pi"
hypr_file="$HOME/.config/hypr/hyprland.lua"
shell_file="$HOME/.config/omarchy/shell.json"
terminal_file="$HOME/.config/xdg-terminals.list"

for path in "$release_dir" "$current_link" "$env_file" "$hypr_file" "$shell_file" "$terminal_file"; do
  if [[ -e $path || -L $path ]]; then
    echo "Refusing to replace existing path: $path" >&2
    exit 1
  fi
done

umask 077
mkdir -p "$data_dir/releases"
pending_dir=$(mktemp -d "$data_dir/releases/.pending.XXXXXXXX")
complete=0
cleanup() {
  local status=$?
  if (( complete == 0 )); then
    rm -f -- "$current_link" "$env_file" "$hypr_file" "$shell_file" "$terminal_file"
    rm -rf -- "$pending_dir" "$release_dir"
  fi
  return "$status"
}
trap cleanup EXIT

git -C "$source_dir" archive --format=tar HEAD | tar -xf - -C "$pending_dir"
mv -- "$pending_dir" "$release_dir"
printf '%s\n' "$revision" >"$release_dir/.omarchy-pi-source-commit"

install -Dm644 "$release_dir/install/arm64/session/90-omarchy-pi" "$env_file"
install -Dm644 "$release_dir/install/arm64/session/hyprland.lua" "$hypr_file"
install -Dm644 "$release_dir/install/arm64/session/shell.json" "$shell_file"
install -Dm644 "$release_dir/install/arm64/session/xdg-terminals.list" "$terminal_file"
ln -s "releases/$revision" "$current_link"

complete=1
echo "Staged Omarchy Pi session from $revision"
echo "No package, system service, or running session was changed."
