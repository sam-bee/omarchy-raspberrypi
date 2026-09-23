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
link_dir=""
link_identity=""
publish_file() {
  local source=$1 target=$2 temp identity digest
  mkdir -p -- "$(dirname -- "$target")"
  temp=$(mktemp "$(dirname -- "$target")/.omarchy-pi.XXXXXXXX")
  install -m644 -- "$source" "$temp"
  identity=$(stat -c '%d:%i' "$temp")
  digest=$(sha256sum "$temp" | cut -d' ' -f1)
  # Record ownership before the atomic link. If a second writer wins the
  # target name, cleanup will see a different inode and leave their file.
  created_paths+=("$temp" "$target")
  created_ids+=("$identity" "$identity")
  created_hashes+=("$digest" "$digest")
  ln -- "$temp" "$target"
}
cleanup() {
  local status=$?
  if (( complete == 0 )); then
    local index path current_id current_hash
    if [[ -n $link_identity && -L $current_link && $(stat -c '%d:%i' "$current_link") == "$link_identity" && $(readlink "$current_link") == "releases/$revision" ]]; then
      rm -f -- "$current_link"
    fi
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
  else
    # The published files are hard links to these temporary inodes. Drop only
    # the staging names, leaving the published links intact.
    local index path
    for (( index=0; index<${#created_paths[@]}; index+=2 )); do
      path=${created_paths[index]}
      if [[ -f $path && ! -L $path && $(stat -c '%d:%i' "$path") == "${created_ids[index]}" ]]; then
        rm -f -- "$path"
      fi
    done
  fi
  if [[ -n $link_dir ]]; then rm -rf -- "$link_dir"; fi
  return "$status"
}
trap cleanup EXIT

git -C "$source_dir" archive --format=tar "$revision" | tar -xf - -C "$pending_dir"
mv -Tn -- "$pending_dir" "$release_dir"
if [[ -e $pending_dir ]]; then
  echo "Another writer created the release path: $release_dir" >&2
  exit 1
fi
printf '%s\n' "$revision" >"$release_dir/.omarchy-pi-source-commit"

publish_file "$release_dir/install/arm64/session/90-omarchy-pi" "$env_file"
publish_file "$release_dir/install/arm64/session/hyprland.lua" "$hypr_file"
publish_file "$release_dir/install/arm64/session/shell.json" "$shell_file"
publish_file "$release_dir/install/arm64/session/xdg-terminals.list" "$terminal_file"

link_dir=$(mktemp -d "$data_dir/.link.XXXXXXXX")
ln -s "releases/$revision" "$link_dir/current"
link_identity=$(stat -c '%d:%i' "$link_dir/current")
ln -P -- "$link_dir/current" "$current_link"

complete=1
echo "Staged Omarchy Pi session from $revision"
echo "No package, system service, or running session was changed."
