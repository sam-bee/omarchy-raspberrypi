#!/bin/bash

# Stage or upgrade a user's minimal graphical session. Package and system
# service changes belong to separately reviewed Pi transactions.
set -euo pipefail

usage() {
  echo "Usage: $0 [--rollback]" >&2
  exit 2
}

mode=stage
if (( $# == 1 )) && [[ $1 == "--rollback" ]]; then
  mode=rollback
elif (( $# != 0 )); then
  usage
fi
if (( EUID == 0 )); then
  echo "Run as the target user without sudo" >&2
  exit 2
fi
if [[ -z ${HOME:-} || $HOME != /* ]]; then
  echo "HOME must be an absolute path" >&2
  exit 2
fi
if [[ -n ${XDG_CONFIG_HOME:-} && ${XDG_CONFIG_HOME%/} != "$HOME/.config" ]]; then
  echo "This minimal session requires XDG_CONFIG_HOME to be unset or $HOME/.config" >&2
  exit 2
fi

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
revision=""
if [[ $mode == "stage" ]]; then
  revision=$(git -C "$source_dir" rev-parse --verify HEAD^{commit})
  if [[ ! $revision =~ ^[0-9a-f]{40}$ ]]; then
    echo "Could not resolve a full source commit" >&2
    exit 1
  fi
  if [[ -n $(git -C "$source_dir" status --porcelain --untracked-files=all) ]]; then
    echo "Commit the source tree before staging a versioned session" >&2
    exit 1
  fi
fi

data_dir="$HOME/.local/share/omarchy-pi"
releases_dir="$data_dir/releases"
current_link="$data_dir/current"
previous_link="$data_dir/previous"
lock_file="$data_dir/.stage.lock"
transaction_dir="$data_dir/.transaction"
pending_transaction=""
pending_release=""
config_keys=(env hypr shell terminal)

die() {
  echo "$*" >&2
  exit 1
}

config_target() {
  case $1 in
    env) printf '%s\n' "$HOME/.config/uwsm/env.d/90-omarchy-pi" ;;
    hypr) printf '%s\n' "$HOME/.config/hypr/hyprland.lua" ;;
    shell) printf '%s\n' "$HOME/.config/omarchy/shell.json" ;;
    terminal) printf '%s\n' "$HOME/.config/xdg-terminals.list" ;;
    *) return 2 ;;
  esac
}

config_payload() {
  local release=$1
  case $2 in
    env) printf '%s\n' "$release/install/arm64/session/90-omarchy-pi" ;;
    hypr) printf '%s\n' "$release/install/arm64/session/hyprland.lua" ;;
    shell) printf '%s\n' "$release/install/arm64/session/shell.json" ;;
    terminal) printf '%s\n' "$release/install/arm64/session/xdg-terminals.list" ;;
    *) return 2 ;;
  esac
}

path_exists() {
  [[ -e $1 || -L $1 ]]
}

file_digest() {
  sha256sum < "$1" | cut -d ' ' -f1
}

file_identity() {
  stat -c '%d:%i' -- "$1"
}

valid_release_target() {
  local target=$1 release marker key payload
  [[ $target =~ ^releases/[0-9a-f]{40}$ ]] || return 1
  release="$data_dir/$target"
  [[ -d $release && ! -L $release ]] || return 1
  marker="$release/.omarchy-pi-source-commit"
  [[ -f $marker && ! -L $marker ]] || return 1
  [[ $(cat -- "$marker") == "${target#releases/}" ]] || return 1
  for key in "${config_keys[@]}"; do
    payload=$(config_payload "$release" "$key")
    [[ -f $payload && ! -L $payload ]] || return 1
  done
}

read_pointer() {
  local path=$1 target
  if ! path_exists "$path"; then
    printf '%s\n' '-'
    return 0
  fi
  [[ -L $path ]] || die "Refusing a non-symlink release pointer: $path"
  target=$(readlink -- "$path")
  valid_release_target "$target" || die "Refusing an invalid release pointer: $path"
  printf '%s\n' "$target"
}

write_pointer() {
  local path=$1 target=$2 tmp_dir
  if [[ $target == "-" ]]; then
    rm -f -- "$path"
    return 0
  fi
  tmp_dir=$(mktemp -d "$data_dir/.pointer.XXXXXXXX")
  if ! ln -s -- "$target" "$tmp_dir/pointer"; then
    rmdir -- "$tmp_dir"
    return 1
  fi
  if ! mv -Tf -- "$tmp_dir/pointer" "$path"; then
    rm -rf -- "$tmp_dir"
    return 1
  fi
  rmdir -- "$tmp_dir"
}

prepare_release() {
  local target=$1 release="$releases_dir/$1" marker
  if path_exists "$release"; then
    [[ -d $release && ! -L $release ]] || die "Refusing an existing non-directory release path: $release"
    valid_release_target "releases/$target" || die "Refusing an incomplete or mismatched release: $release"
    return 0
  fi

  pending_release=$(mktemp -d "$releases_dir/.pending.XXXXXXXX")
  git -C "$source_dir" archive --format=tar "$target" | tar -xf - -C "$pending_release"
  marker="$pending_release/.omarchy-pi-source-commit"
  printf '%s\n' "$target" > "$marker"
  mv -Tn -- "$pending_release" "$release"
  if path_exists "$pending_release"; then
    [[ -d $release && ! -L $release ]] || die "Another writer created an invalid release path: $release"
    valid_release_target "releases/$target" || die "Another writer created a mismatched release: $release"
    rm -rf -- "$pending_release"
  fi
  pending_release=""
}

restore_pointer_if_owned() {
  local path=$1 old_target=$2 new_target=$3 current_target
  if path_exists "$path"; then
    if [[ ! -L $path ]]; then
      echo "Recovery found a changed release pointer and retained its journal: $path" >&2
      return 1
    fi
    current_target=$(readlink -- "$path")
  else
    current_target="-"
  fi

  if [[ $current_target == "$new_target" ]]; then
    write_pointer "$path" "$old_target" || return 1
  elif [[ $current_target == "$old_target" ]]; then
    :
  else
    echo "Recovery found a changed release pointer and retained its journal: $path" >&2
    return 1
  fi
}

verify_committed_pointers() {
  local tx=$1 expected_current expected_previous actual_current actual_previous
  expected_current=$(cat -- "$tx/new-current")
  expected_previous=$(cat -- "$tx/new-previous")
  actual_current="-"
  actual_previous="-"
  if path_exists "$current_link"; then
    [[ -L $current_link ]] || return 1
    actual_current=$(readlink -- "$current_link")
  fi
  if path_exists "$previous_link"; then
    [[ -L $previous_link ]] || return 1
    actual_previous=$(readlink -- "$previous_link")
  fi
  [[ $actual_current == "$expected_current" && $actual_previous == "$expected_previous" ]] || return 1
  if [[ $expected_current != "-" ]]; then valid_release_target "$expected_current" || return 1; fi
  if [[ $expected_previous != "-" ]]; then valid_release_target "$expected_previous" || return 1; fi
}

restore_config_if_owned() {
  local tx=$1 key=$2 target action new_identity new_digest old_digest backup restore_temp current_identity current_digest
  target=$(config_target "$key")
  action=$(cat -- "$tx/actions/$key")
  case $action in
    replace)
      new_identity=$(cat -- "$tx/new-identities/$key")
      new_digest=$(cat -- "$tx/new-digests/$key")
      old_digest=$(cat -- "$tx/old-digests/$key")
      backup="$tx/backups/$key"
      if [[ -f $target && ! -L $target ]]; then
        current_identity=$(file_identity "$target")
        current_digest=$(file_digest "$target")
        if [[ $current_digest == "$old_digest" ]]; then
          :
        elif [[ $current_identity == "$new_identity" && $current_digest == "$new_digest" ]]; then
          restore_temp=$(mktemp "$(dirname -- "$target")/.omarchy-pi-recover.XXXXXXXX")
          if ! cp -p -- "$backup" "$restore_temp" || [[ $(file_digest "$restore_temp") != "$old_digest" ]]; then
            rm -f -- "$restore_temp"
            echo "Recovery backup failed its digest check and the journal was retained: $target" >&2
            return 1
          fi
          if ! mv -Tf -- "$restore_temp" "$target"; then
            rm -f -- "$restore_temp"
            return 1
          fi
        elif [[ $current_identity == "$new_identity" ]]; then
          echo "Recovery found an edited config and retained its journal: $target" >&2
          return 1
        else
          echo "Recovery found a changed config and retained its journal: $target" >&2
          return 1
        fi
      else
        echo "Recovery found a missing config and retained its journal: $target" >&2
        return 1
      fi
      ;;
    create)
      new_identity=$(cat -- "$tx/new-identities/$key")
      new_digest=$(cat -- "$tx/new-digests/$key")
      if [[ -f $target && ! -L $target && $(file_identity "$target") == "$new_identity" ]]; then
        if [[ $(file_digest "$target") == "$new_digest" ]]; then
          rm -f -- "$target"
        else
          echo "Recovery found an edited config and retained its journal: $target" >&2
          return 1
        fi
      elif path_exists "$target"; then
        echo "Recovery found a replaced config and retained its journal: $target" >&2
        return 1
      fi
      ;;
    preserve|missing|keep) ;;
    *) die "Unknown action in the interrupted session transaction: $action" ;;
  esac
}

cleanup_staged_artifacts() {
  local tx=$1 key path identity
  for key in "${config_keys[@]}"; do
    [[ -f $tx/staged-paths/$key && -f $tx/new-identities/$key ]] || continue
    path=$(cat -- "$tx/staged-paths/$key")
    identity=$(cat -- "$tx/new-identities/$key")
    case $path in
      "$(dirname -- "$(config_target "$key")")/.omarchy-pi-stage."*) ;;
      *)
        echo "Recovery found an invalid staging path and retained its journal: $path" >&2
        return 1
        ;;
    esac
    if [[ -f $path && ! -L $path ]]; then
      if [[ $(file_identity "$path") == "$identity" ]]; then
        rm -f -- "$path" || return 1
      else
        echo "Recovery found a changed staging file and retained its journal: $path" >&2
        return 1
      fi
    fi
  done
}

recover_transaction() {
  local tx=$transaction_dir old_current old_previous new_current new_previous key target failed=0
  [[ ! -L $tx ]] || die "Refusing a symlinked transaction journal: $tx"
  [[ -d $tx ]] || return 0
  [[ -f $tx/READY && ! -L $tx/READY ]] || die "Incomplete transaction journal at $tx; inspect it before continuing"
  if [[ -f $tx/COMMITTED ]]; then
    if ! verify_committed_pointers "$tx"; then
      echo "Committed session pointers do not match the journal; retaining $tx for inspection." >&2
      return 1
    fi
    cleanup_staged_artifacts "$tx" || return 1
    rm -rf -- "$tx"
    return 0
  fi

  old_current=$(cat -- "$tx/old-current")
  old_previous=$(cat -- "$tx/old-previous")
  new_current=$(cat -- "$tx/new-current")
  new_previous=$(cat -- "$tx/new-previous")
  for target in "$old_current" "$old_previous" "$new_current" "$new_previous"; do
    if [[ $target != "-" ]]; then valid_release_target "$target" || die "Invalid release target in transaction journal: $target"; fi
  done
  restore_pointer_if_owned "$current_link" "$old_current" "$new_current" || failed=1
  restore_pointer_if_owned "$previous_link" "$old_previous" "$new_previous" || failed=1
  for key in "${config_keys[@]}"; do
    restore_config_if_owned "$tx" "$key" || failed=1
  done
  cleanup_staged_artifacts "$tx" || failed=1
  if (( failed != 0 )); then
    echo "Session recovery is incomplete; the journal remains at $tx for inspection." >&2
    return 1
  fi
  rm -rf -- "$tx"
  echo "Recovered an interrupted Omarchy Pi session transaction." >&2
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM HUP
  if [[ -L $transaction_dir ]]; then
    echo "Refusing a symlinked transaction journal: $transaction_dir" >&2
    status=1
  elif [[ -d $transaction_dir ]]; then
    if [[ -f $transaction_dir/COMMITTED ]]; then
      if ! verify_committed_pointers "$transaction_dir"; then
        echo "Committed session pointers do not match the journal; retaining $transaction_dir for inspection." >&2
        status=1
      elif ! cleanup_staged_artifacts "$transaction_dir"; then
        status=1
      else
        rm -rf -- "$transaction_dir" || status=1
      fi
    else
      recover_transaction || {
        echo "Session recovery is incomplete. Re-run this command after inspecting $transaction_dir." >&2
        status=1
      }
    fi
  fi
  if [[ -n $pending_transaction && -d $pending_transaction ]]; then
    cleanup_staged_artifacts "$pending_transaction" || status=1
    rm -rf -- "$pending_transaction" || status=1
  fi
  if [[ -n $pending_release && -d $pending_release ]]; then
    rm -rf -- "$pending_release" || status=1
  fi
  return "$status"
}

umask 077
if [[ -L $data_dir || -L $releases_dir ]]; then
  die "Refusing a symlinked session data directory"
fi
mkdir -p -- "$releases_dir"
[[ $(stat -c '%u' -- "$data_dir") == "$EUID" && $(stat -c '%u' -- "$releases_dir") == "$EUID" ]] || die "Session release directories must be owned by the target user"
if [[ -L $lock_file ]]; then
  die "Refusing a symlinked staging lock: $lock_file"
fi
exec {lock_fd}>"$lock_file"
if ! flock -n "$lock_fd"; then
  die "Another Omarchy Pi staging operation holds $lock_file"
fi
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

recover_transaction
for orphan in "$data_dir"/.transaction.pending.*; do
  if [[ -d $orphan && ! -L $orphan ]]; then rm -rf -- "$orphan"; fi
done

old_current=$(read_pointer "$current_link")
old_previous=$(read_pointer "$previous_link")
if [[ $old_previous != "-" && $old_previous == "$old_current" ]]; then
  die "The previous release pointer is the same as the current release"
fi

if [[ $mode == "rollback" ]]; then
  [[ $old_current != "-" ]] || die "No staged Omarchy Pi session is active"
  [[ $old_previous != "-" ]] || die "No previous Omarchy Pi session release is available"
  next_target=$old_previous
else
  next_target="releases/$revision"
  if [[ $old_current == "$next_target" ]]; then
    die "This source revision is already active; refusing a no-op stage"
  fi
  if [[ $old_current == "-" ]]; then
    for key in "${config_keys[@]}"; do
      target=$(config_target "$key")
      if path_exists "$target"; then
        die "Refusing to replace existing path: $target"
      fi
    done
    if [[ $old_previous != "-" ]]; then die "A previous release exists without a current release"; fi
  fi
fi

next_release="$data_dir/$next_target"
if [[ $mode == "stage" ]]; then
  prepare_release "$revision"
else
  valid_release_target "$next_target" || die "The previous release is incomplete: $next_release"
fi

pending_transaction=$(mktemp -d "$data_dir/.transaction.pending.XXXXXXXX")
mkdir -p -- "$pending_transaction/actions" "$pending_transaction/backups" "$pending_transaction/staged-paths" "$pending_transaction/old-digests" "$pending_transaction/old-identities" "$pending_transaction/new-digests" "$pending_transaction/new-identities"
printf '%s\n' "$old_current" > "$pending_transaction/old-current"
printf '%s\n' "$old_previous" > "$pending_transaction/old-previous"
printf '%s\n' "$next_target" > "$pending_transaction/new-current"
printf '%s\n' "$old_current" > "$pending_transaction/new-previous"

old_release=""
if [[ $old_current != "-" ]]; then old_release="$data_dir/$old_current"; fi
for key in "${config_keys[@]}"; do
  target=$(config_target "$key")
  new_payload=$(config_payload "$next_release" "$key")
  old_payload=""
  if [[ -n $old_release ]]; then old_payload=$(config_payload "$old_release" "$key"); fi
  action=""
  if path_exists "$target"; then
    if [[ $mode == "stage" && $old_current == "-" ]]; then
      die "Refusing to replace existing path: $target"
    fi
    if [[ -n $old_payload && -f $old_payload && ! -L $old_payload && -f $target && ! -L $target && $(stat -c '%u' -- "$target") == "$EUID" ]] && cmp -s -- "$target" "$old_payload"; then
      if cmp -s -- "$target" "$new_payload"; then
        action=keep
      else
        action=replace
      fi
    else
      action=preserve
    fi
  else
    if [[ $old_current == "-" || ! -e $old_payload ]]; then
      action=create
    else
      action=missing
    fi
  fi
  printf '%s\n' "$action" > "$pending_transaction/actions/$key"
  if [[ $action == "replace" ]]; then
    cp -p -- "$target" "$pending_transaction/backups/$key"
    printf '%s\n' "$(file_digest "$target")" > "$pending_transaction/old-digests/$key"
    printf '%s\n' "$(file_identity "$target")" > "$pending_transaction/old-identities/$key"
  fi
  if [[ $action == "replace" || $action == "create" ]]; then
    mkdir -p -- "$(dirname -- "$target")"
    staged_path=$(mktemp "$(dirname -- "$target")/.omarchy-pi-stage.XXXXXXXX")
    printf '%s\n' "$staged_path" > "$pending_transaction/staged-paths/$key"
    install -m 644 -- "$new_payload" "$staged_path"
    printf '%s\n' "$(file_digest "$staged_path")" > "$pending_transaction/new-digests/$key"
    printf '%s\n' "$(file_identity "$staged_path")" > "$pending_transaction/new-identities/$key"
  fi
done
printf 'ready\n' > "$pending_transaction/READY"
mv -Tn -- "$pending_transaction" "$transaction_dir"
if [[ -e $pending_transaction ]]; then die "Another staging operation created a transaction journal"; fi
pending_transaction=""

for key in "${config_keys[@]}"; do
  action=$(cat -- "$transaction_dir/actions/$key")
  target=$(config_target "$key")
  case $action in
    replace)
      old_digest=$(cat -- "$transaction_dir/old-digests/$key")
      old_identity=$(cat -- "$transaction_dir/old-identities/$key")
      [[ -f $target && ! -L $target && $(file_identity "$target") == "$old_identity" && $(file_digest "$target") == "$old_digest" ]] || die "Config changed during staging; preserving it: $target"
      mv -Tf -- "$(cat -- "$transaction_dir/staged-paths/$key")" "$target"
      ;;
    create)
      ln -T -- "$(cat -- "$transaction_dir/staged-paths/$key")" "$target"
      ;;
    preserve|missing|keep) ;;
    *) die "Unknown config action in the session transaction: $action" ;;
  esac
done

write_pointer "$previous_link" "$old_current"
write_pointer "$current_link" "$next_target"
printf 'committed\n' > "$transaction_dir/COMMITTED"
if [[ $mode == "rollback" ]]; then
  echo "Rolled back the Omarchy Pi session to ${next_target#releases/}."
else
  echo "Staged Omarchy Pi session from $revision."
fi
echo "Modified or missing user configs were preserved; package, system service, and running session state were not changed."
