#!/bin/bash

# Stage only a user's minimal graphical session. Package and service changes
# belong to separately reviewed Pi transactions.
set -euo pipefail

if (( $# != 0 || EUID == 0 )); then
  echo "Run as the target user without arguments or sudo" >&2
  exit 2
fi
if [[ -n ${XDG_CONFIG_HOME:-} && ${XDG_CONFIG_HOME%/} != "$HOME/.config" ]]; then
  echo "This minimal session requires XDG_CONFIG_HOME to be unset or $HOME/.config" >&2
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
lock_file="$data_dir/.stage.lock"
env_file="$HOME/.config/uwsm/env.d/90-omarchy-pi"
hypr_file="$HOME/.config/hypr/hyprland.lua"
shell_file="$HOME/.config/omarchy/shell.json"
terminal_file="$HOME/.config/xdg-terminals.list"

umask 077
mkdir -p "$data_dir/releases"
if [[ -L $lock_file ]]; then
  echo "Refusing a symlinked staging lock: $lock_file" >&2
  exit 1
fi
exec {lock_fd}>"$lock_file"
if ! flock -n "$lock_fd"; then
  echo "Another Omarchy Pi staging operation holds $lock_file" >&2
  exit 1
fi

for path in "$release_dir" "$current_link" "$env_file" "$hypr_file" "$shell_file" "$terminal_file"; do
  if [[ -e $path || -L $path ]]; then
    echo "Refusing to replace existing path: $path" >&2
    exit 1
  fi
done

pending_dir=$(mktemp -d "$data_dir/releases/.pending.XXXXXXXX")
complete=0
created_paths=()
created_ids=()
created_hashes=()
record_file() {
  local identity digest
  identity=$(stat -c '%d:%i' "$1")
  digest=$(sha256sum "$1" | cut -d' ' -f1)
  created_paths+=("$1")
  created_ids+=("$identity")
  created_hashes+=("$digest")
}
cleanup() {
  local status=$?
  if (( complete == 0 )); then
    local index path current_id current_hash
    for (( index=${#created_paths[@]}-1; index>=0; index-- )); do
      path=${created_paths[index]}
      if [[ -f $path && ! -L $path ]]; then
        current_id=$(stat -c '%d:%i' "$path")
        current_hash=$(sha256sum "$path" | cut -d' ' -f1)
        if [[ $current_id == "${created_ids[index]}" && $current_hash == "${created_hashes[index]}" ]]; then
          rm -f -- "$path"
        fi
      fi
    done
    rm -rf -- "$pending_dir"
    # Leave a promoted release in place on failure: another process might
    # have added files to it, so recursive cleanup is not ownership-safe.
  fi
  return "$status"
}
trap cleanup EXIT

git -C "$source_dir" archive --format=tar HEAD | tar -xf - -C "$pending_dir"
mv -- "$pending_dir" "$release_dir"
printf '%s\n' "$revision" >"$release_dir/.omarchy-pi-source-commit"

install -Dm644 "$release_dir/install/arm64/session/90-omarchy-pi" "$env_file"
record_file "$env_file"
install -Dm644 "$release_dir/install/arm64/session/hyprland.lua" "$hypr_file"
record_file "$hypr_file"
install -Dm644 "$release_dir/install/arm64/session/shell.json" "$shell_file"
record_file "$shell_file"
install -Dm644 "$release_dir/install/arm64/session/xdg-terminals.list" "$terminal_file"
record_file "$terminal_file"
ln -s "releases/$revision" "$current_link"

complete=1
echo "Staged Omarchy Pi session from $revision"
echo "No package, system service, or running session was changed."
