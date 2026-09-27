#!/bin/bash

# Populate a fresh ARM target root with the Omarchy Pi desktop payload.
#
# This is an image/installer operation. It does not connect to a Pi, run a
# desktop session, copy a home directory, or copy machine credentials. With
# no --user it prepares /etc/skel so the installer can choose the account later.
# With --user it creates one new target-local account and seeds that account.
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: provision-desktop-root.sh --rootfs ROOT --source-checkout DIR
       [--payload-dir DIR] [--user NAME] [--home /absolute/path] [--dry-run]

Populate a fresh target root with the Omarchy source and Pi desktop helpers.
The source checkout must be a clean Git checkout. --payload-dir may contain
packages/desktop-manifest.json (or desktop-manifest.json) from the package
payload builder; it is copied into the target as provenance.

Without --user, new-user files are installed into ROOT/etc/skel. With --user,
the account is created in ROOT and the same files are seeded into its home.
Passwords, SSH keys, machine-id and boot settings are configured by the
installer/first boot and are never accepted by this command.
USAGE
  exit 2
}

die() {
  echo "provision-desktop-root: $*" >&2
  exit 1
}

rootfs=""
source_checkout=""
payload_dir=""
selected_user=""
selected_home=""
dry_run=0

while (( $# )); do
  case "$1" in
    --rootfs|--target-root)
      (( $# >= 2 )) || usage
      rootfs=$2
      shift 2
      ;;
    --source-checkout|--source-root|--payload-source)
      (( $# >= 2 )) || usage
      source_checkout=$2
      shift 2
      ;;
    --payload-dir)
      (( $# >= 2 )) || usage
      payload_dir=$2
      shift 2
      ;;
    --user)
      (( $# >= 2 )) || usage
      selected_user=$2
      shift 2
      ;;
    --home)
      (( $# >= 2 )) || usage
      selected_home=$2
      shift 2
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      ;;
  esac
done

(( EUID == 0 || dry_run )) || die "apply mode must run as root"
[[ -n $rootfs && -n $source_checkout ]] || usage

absolute_path() {
  local path=$1
  [[ $path == /* ]] || die "path must be absolute: $path"
  path=$(realpath -m -s -- "$path") || die "could not normalize path: $path"
  if [[ $path == / ]]; then
    printf '/\n'
  else
    printf '%s\n' "${path%/}"
  fi
}

rootfs=$(absolute_path "$rootfs")
source_checkout=$(absolute_path "$source_checkout")
[[ -z $payload_dir ]] || payload_dir=$(absolute_path "$payload_dir")

reject_symlink_components() {
  local path=$1 include_leaf=${2:-1} current component index
  local -a components=()
  IFS=/ read -r -a components <<<"${path#/}"
  current="/"
  local last=${#components[@]}
  (( include_leaf )) || (( last-- ))
  for (( index=0; index < last; index++ )); do
    component=${components[index]}
    [[ -n $component ]] || continue
    current+="$component"
    [[ ! -L $current ]] || die "refusing symlink path component: $current"
    current+=/
  done
}

require_real_directory() {
  local path=$1 description=$2
  reject_symlink_components "$path"
  [[ -d $path && ! -L $path ]] || die "$description must be a real directory: $path"
}

require_regular_file() {
  local path=$1 description=$2
  reject_symlink_components "$path"
  [[ -f $path && ! -L $path ]] || die "$description must be a regular file: $path"
}

require_target_root() {
  require_real_directory "$rootfs" "target root"
  [[ $rootfs != / ]] || die "refusing the host root"
  case "$rootfs/" in
    /dev/*|/proc/*|/sys/*|/run/*|/boot/*)
      die "refusing target root under a live system directory: $rootfs"
      ;;
  esac
  [[ -f $rootfs/etc/passwd ]] || die "target root is missing etc/passwd"
  [[ -f $rootfs/etc/group ]] || die "target root is missing etc/group"
}

require_source_checkout() {
  require_real_directory "$source_checkout" "source checkout"
  local top revision status path
  top=$(git -C "$source_checkout" rev-parse --show-toplevel 2>/dev/null) ||
    die "source checkout is not a Git work tree"
  top=$(cd -- "$top" && pwd -P)
  [[ $top == "$source_checkout" ]] || die "source path must be the checkout root: $top"
  status=$(git -C "$source_checkout" status --porcelain --untracked-files=all) ||
    die "could not inspect source checkout"
  [[ -z $status ]] || die "source checkout must be clean"
  revision=$(git -C "$source_checkout" rev-parse --verify HEAD^{commit}) ||
    die "source checkout has no commit"
  [[ $revision =~ ^[0-9a-f]{40}$ ]] || die "source revision is not a full commit"
  SOURCE_REVISION=$revision

  # A source archive must never contain the project's credential area or a
  # common environment-secret file. Git archive already omits ignored files;
  # this check also catches accidentally committed secrets before extraction.
  while IFS= read -r path; do
    case "$path" in
      credentials/*|*.env|.env|.env.*|*/.env|*/.env.*)
        die "source checkout contains a credential-like path: $path"
        ;;
    esac
  done < <(git -C "$source_checkout" ls-files)
}

require_payload() {
  PAYLOAD_MANIFEST=""
  [[ -z $payload_dir ]] && return 0
  require_real_directory "$payload_dir" "payload directory"
  local candidate
  for candidate in \
    "$payload_dir/packages/desktop-manifest.json" \
    "$payload_dir/desktop-manifest.json" \
    "$payload_dir/package-manifest.json"; do
    if [[ -e $candidate || -L $candidate ]]; then
      require_regular_file "$candidate" "package manifest"
      PAYLOAD_MANIFEST=$candidate
      break
    fi
  done
  [[ -n $PAYLOAD_MANIFEST ]] ||
    echo "provision-desktop-root: payload has no package manifest; continuing with source payload" >&2
}

target_path() {
  local relative=$1
  [[ $relative == /* ]] || die "internal target path is not absolute: $relative"
  local path
  path=$(realpath -m -s -- "$rootfs$relative") || die "could not normalize target path: $relative"
  case "$path" in
    "$rootfs"/*) ;;
    *) die "internal target path escapes the root: $relative" ;;
  esac
  reject_symlink_components "$path" 0
  printf '%s\n' "$path"
}

announce() {
  if (( dry_run )); then
    echo "provision-desktop-root: would $*"
  else
    echo "provision-desktop-root: $*"
  fi
}

install_file() {
  local source=$1 destination=$2 mode=$3 owner_uid=${4:-0} owner_gid=${5:-0}
  require_regular_file "$source" "source file"
  reject_symlink_components "$destination" 0
  if [[ -e $destination || -L $destination ]]; then
    [[ -f $destination && ! -L $destination ]] || die "refusing to replace non-file: $destination"
    cmp -s -- "$source" "$destination" || die "target file differs; refusing to overwrite: $destination"
    if (( ! dry_run )); then
      chmod "$mode" -- "$destination"
      chown "$owner_uid:$owner_gid" -- "$destination"
    fi
    return 0
  fi
  announce "install $destination"
  (( dry_run )) && return 0
  mkdir -p -- "$(dirname -- "$destination")"
  install -m "$mode" -- "$source" "$destination"
  chown "$owner_uid:$owner_gid" -- "$destination"
}

install_text() {
  local destination=$1 mode=$2 owner_uid=$3 owner_gid=$4 content=$5
  reject_symlink_components "$destination" 0
  if [[ -e $destination || -L $destination ]]; then
    [[ -f $destination && ! -L $destination ]] || die "refusing to replace non-file: $destination"
    local existing_tmp
    existing_tmp=$(mktemp)
    printf '%s' "$content" >"$existing_tmp"
    if ! cmp -s -- "$existing_tmp" "$destination"; then
      rm -f -- "$existing_tmp"
      die "target file differs; refusing to overwrite: $destination"
    fi
    rm -f -- "$existing_tmp"
    return 0
  fi
  announce "write $destination"
  (( dry_run )) && return 0
  mkdir -p -- "$(dirname -- "$destination")"
  local temporary
  temporary=$(mktemp "$(dirname -- "$destination")/.desktop-provision.XXXXXXXX")
  printf '%s' "$content" >"$temporary"
  chmod "$mode" -- "$temporary"
  chown "$owner_uid:$owner_gid" -- "$temporary"
  mv -T -- "$temporary" "$destination"
}

install_symlink() {
  local target=$1 destination=$2 owner_uid=${3:-0} owner_gid=${4:-0}
  reject_symlink_components "$destination" 0
  if [[ -e $destination || -L $destination ]]; then
    [[ -L $destination && $(readlink -- "$destination") == "$target" ]] ||
      die "target symlink differs; refusing to overwrite: $destination"
    return 0
  fi
  announce "link $destination -> $target"
  (( dry_run )) && return 0
  mkdir -p -- "$(dirname -- "$destination")"
  ln -s -- "$target" "$destination"
  chown -h "$owner_uid:$owner_gid" -- "$destination"
}

stage_source_tree() {
  local destination
  destination=$(target_path /usr/share/omarchy-pi)
  if [[ -e $destination || -L $destination ]]; then
    [[ -d $destination && ! -L $destination ]] || die "Omarchy source destination is not a directory"
    [[ -f $destination/.source-revision ]] || die "existing Omarchy source has no revision marker"
    [[ $(<"$destination/.source-revision") == "$SOURCE_REVISION" ]] ||
      die "existing Omarchy source has a different revision: $destination"
    announce "reuse Omarchy source revision $SOURCE_REVISION"
    return 0
  fi
  announce "extract clean Omarchy source revision $SOURCE_REVISION into $destination"
  (( dry_run )) && return 0
  reject_symlink_components "$destination" 0
  mkdir -p -- "$destination"
  git -C "$source_checkout" archive --format=tar "$SOURCE_REVISION" |
    tar -xf - -C "$destination" --no-same-owner --no-same-permissions
  install_text "$destination/.source-revision" 0644 0 0 "$SOURCE_REVISION"$'\n'
}

stage_system_assets() {
  local source_root="$source_checkout/install/arm64"
  local source
  for source in \
    "$source_root/session/systemd/start-uwsm-session.sh" \
    "$source_root/session/systemd/verify-hypr-rdp-runtime.py" \
    "$source_root/session/ensure-headless-output.sh" \
    "$source_root/session/omarchy-lock-password" \
    "$source_root/session/fresh-hyprland-prefix.lua"; do
    require_regular_file "$source" "Pi session helper"
  done
  install_file "$source_root/session/systemd/start-uwsm-session.sh" \
    "$(target_path /usr/local/libexec/omarchy-pi/start-uwsm-session.sh)" 0755
  install_file "$source_root/session/systemd/verify-hypr-rdp-runtime.py" \
    "$(target_path /usr/local/libexec/omarchy-pi/verify-hypr-rdp-runtime.py)" 0755
  install_file "$source_root/session/ensure-headless-output.sh" \
    "$(target_path /usr/local/libexec/omarchy-pi/ensure-headless-output.sh)" 0755
  install_file "$source_root/session/systemd/omarchy-pi-uwsm-session@.service" \
    "$(target_path /etc/systemd/system/omarchy-pi-uwsm-session@.service)" 0644
  install_file "$source_root/session/systemd/omarchy-pi-hypr-rdp.service" \
    "$(target_path /etc/systemd/user/omarchy-pi-hypr-rdp.service)" 0644
  install_file "$source_root/session/omarchy-lock-password" \
    "$(target_path /etc/pam.d/omarchy-lock-password)" 0644
}

stage_user_defaults() {
  local destination="$rootfs/etc/skel/.config/hypr" source content
  for source in "$source_checkout"/config/hypr/*; do
    [[ -f $source && ! -L $source ]] || continue
    [[ $(basename -- "$source") == hyprland.lua ]] && continue
    install_file "$source" "$destination/$(basename -- "$source")" 0644
  done
  content=$(<"$source_checkout/install/arm64/session/fresh-hyprland-prefix.lua")
  content+=$'\n'
  content+=$(<"$source_checkout/config/hypr/hyprland.lua")
  install_text "$rootfs/etc/skel/.config/hypr/hyprland.lua" 0644 0 0 "$content"$'\n'
  install_file "$source_checkout/config/omarchy/shell.json" \
    "$rootfs/etc/skel/.config/omarchy/shell.json" 0644
  install_file "$source_checkout/install/arm64/session/chromium-flags.conf" \
    "$rootfs/etc/skel/.config/chromium-flags.conf" 0644
  install_file "$source_checkout/install/arm64/session/portals.conf" \
    "$rootfs/etc/skel/.config/xdg-desktop-portal/portals.conf" 0644
  install_file "$source_checkout/install/arm64/session/xdg-terminals.list" \
    "$rootfs/etc/skel/.config/xdg-terminals.list" 0644
  local env_content
  env_content=$(cat <<'EOF'
# Omarchy Pi target environment. The source tree is package-owned.
export OMARCHY_PATH="$HOME/.local/share/omarchy-pi/current"
case ":$PATH:" in
  *":$OMARCHY_PATH/bin:"*) ;;
  *) export PATH="$OMARCHY_PATH/bin:$PATH" ;;
esac
export TERMINAL=xdg-terminal-exec
EOF
)
  install_text "$rootfs/etc/skel/.config/uwsm/env.d/90-omarchy-pi" 0644 0 0 "$env_content"$'\n'
}

target_account_line() {
  local name=$1
  awk -F: -v name="$name" '$1 == name { print; exit }' "$rootfs/etc/passwd"
}

create_target_user() {
  [[ -n $selected_user ]] || return 0
  [[ $selected_user =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "unsupported target username: $selected_user"
  [[ $selected_user != root ]] || die "refusing to provision the root account"
  if [[ -z $selected_home ]]; then
    selected_home="/home/$selected_user"
  fi
  [[ $selected_home == /* && $selected_home != / && $selected_home != *[[:space:]\"\\]* ]] ||
    die "home must be an absolute path without whitespace, quotes or backslashes"
  local home_in_target
  home_in_target=$(realpath -m -s -- "$rootfs$selected_home") || die "could not normalize target home"
  case "$home_in_target" in
    "$rootfs"/*) ;;
    *) die "target home escapes the target root: $selected_home" ;;
  esac
  reject_symlink_components "$home_in_target" 0
  [[ ! -e $home_in_target && ! -L $home_in_target ]] || die "target home already exists: $selected_home"
  [[ -z $(target_account_line "$selected_user") ]] || die "target account already exists: $selected_user"
  if (( dry_run )); then
    announce "create target account $selected_user with home $selected_home"
    return 0
  fi
  useradd --root "$rootfs" --create-home --home-dir "$selected_home" --shell /bin/bash "$selected_user" ||
    die "target user creation failed"
  local account_line uid gid group
  account_line=$(target_account_line "$selected_user")
  [[ $account_line == *:*:*:*:*:*:* ]] || die "target account was not created"
  IFS=: read -r _ _ uid gid _ _ _ <<<"$account_line"
  [[ $uid =~ ^[0-9]+$ && $gid =~ ^[0-9]+$ ]] || die "target account has invalid IDs"
  # These groups are optional across Arch Linux ARM bases. Add only groups
  # already present in the target; group creation belongs to the base image.
  for group in wheel audio video input storage optical network rfkill docker; do
    if awk -F: -v name="$group" '$1 == name { found=1 } END { exit !found }' "$rootfs/etc/group"; then
      usermod --root "$rootfs" --append --groups "$group" "$selected_user" || die "could not add $selected_user to $group"
    fi
  done
  chown "$uid:$gid" -- "$home_in_target"
}

seed_selected_user() {
  [[ -n $selected_user ]] || return 0
  local account_line uid gid home_in_target source content
  account_line=$(target_account_line "$selected_user")
  if (( dry_run )); then
    uid=0
    gid=0
  else
    IFS=: read -r _ _ uid gid _ _ _ <<<"$account_line"
  fi
  home_in_target=$(realpath -m -s -- "$rootfs$selected_home") || die "could not normalize target home"
  for source in "$source_checkout"/config/hypr/*; do
    [[ -f $source && ! -L $source ]] || continue
    [[ $(basename -- "$source") == hyprland.lua ]] && continue
    install_file "$source" "$home_in_target/.config/hypr/$(basename -- "$source")" 0644 "$uid" "$gid"
  done
  content=$(<"$source_checkout/install/arm64/session/fresh-hyprland-prefix.lua")
  content+=$'\n'
  content+=$(<"$source_checkout/config/hypr/hyprland.lua")
  install_text "$home_in_target/.config/hypr/hyprland.lua" 0644 "$uid" "$gid" "$content"$'\n'
  install_file "$source_checkout/config/omarchy/shell.json" "$home_in_target/.config/omarchy/shell.json" 0644 "$uid" "$gid"
  install_file "$source_checkout/install/arm64/session/chromium-flags.conf" "$home_in_target/.config/chromium-flags.conf" 0644 "$uid" "$gid"
  install_file "$source_checkout/install/arm64/session/portals.conf" "$home_in_target/.config/xdg-desktop-portal/portals.conf" 0644 "$uid" "$gid"
  install_file "$source_checkout/install/arm64/session/xdg-terminals.list" "$home_in_target/.config/xdg-terminals.list" 0644 "$uid" "$gid"
  local env_content
  if (( dry_run )); then
    env_content=$(cat <<'EOF'
# Omarchy Pi target environment. The source tree is package-owned.
export OMARCHY_PATH="$HOME/.local/share/omarchy-pi/current"
case ":$PATH:" in
  *":$OMARCHY_PATH/bin:"*) ;;
  *) export PATH="$OMARCHY_PATH/bin:$PATH" ;;
esac
export TERMINAL=xdg-terminal-exec
EOF
)
  else
    env_content=$(<"$rootfs/etc/skel/.config/uwsm/env.d/90-omarchy-pi")
  fi
  install_text "$home_in_target/.config/uwsm/env.d/90-omarchy-pi" 0644 "$uid" "$gid" "$env_content"$'\n'
  local release_dir="$home_in_target/.local/share/omarchy-pi/releases/$SOURCE_REVISION"
  if (( dry_run )); then
    announce "copy Omarchy release $SOURCE_REVISION into $release_dir"
  else
    [[ ! -e $release_dir && ! -L $release_dir ]] || die "target user release already exists: $release_dir"
    mkdir -p -- "$release_dir"
    cp -a -- "$rootfs/usr/share/omarchy-pi/." "$release_dir/"
    install_text "$release_dir/.omarchy-pi-source-commit" 0644 "$uid" "$gid" "$SOURCE_REVISION"$'\n'
    chown -R "$uid:$gid" -- "$release_dir"
  fi
  install_symlink "releases/$SOURCE_REVISION" "$home_in_target/.local/share/omarchy-pi/current" "$uid" "$gid"
  local wants
  wants=$(target_path "/etc/systemd/system/multi-user.target.wants/omarchy-pi-uwsm-session@${selected_user}.service")
  reject_symlink_components "$wants" 0
  if [[ -e $wants || -L $wants ]]; then
    [[ -L $wants && $(readlink -- "$wants") == ../omarchy-pi-uwsm-session@.service ]] ||
      die "existing Pi UWSM enablement differs: $wants"
  else
    announce "enable Pi UWSM session for $selected_user"
    if (( ! dry_run )); then
      mkdir -p -- "$(dirname -- "$wants")"
      ln -s -- ../omarchy-pi-uwsm-session@.service "$wants"
    fi
  fi
}

write_provenance() {
  local manifest package_digest="" user_json="null" home_json="" provenance
  if [[ -n $PAYLOAD_MANIFEST ]]; then
    package_digest=$(sha256sum -- "$PAYLOAD_MANIFEST" | cut -d' ' -f1)
  fi
  if [[ -n $selected_user ]]; then
    user_json="\"$selected_user\""
    home_json="\"$selected_home\""
  else
    home_json="null"
  fi
  provenance=$(cat <<EOF
{
  "schema_version": 1,
  "source_revision": "$SOURCE_REVISION",
  "target_user": $user_json,
  "target_home": $home_json,
  "package_manifest_sha256": "${package_digest:-}",
  "identity_policy": "generate machine and account identities on target installation"
}
EOF
)
  if [[ -n $selected_user ]]; then
    manifest=$(target_path /var/lib/omarchy-pi/desktop-user-provision.json)
  else
    manifest=$(target_path /usr/share/omarchy-pi/desktop-provision.json)
  fi
  install_text "$manifest" 0644 0 0 "$provenance"$'\n'
  if [[ -n $PAYLOAD_MANIFEST ]]; then
    install_file "$PAYLOAD_MANIFEST" "$(target_path /usr/share/omarchy-pi/desktop-package-manifest.json)" 0644
  fi
}

require_target_root
require_source_checkout
require_payload
stage_source_tree
stage_system_assets
stage_user_defaults
create_target_user
seed_selected_user
write_provenance

if (( dry_run )); then
  echo "provision-desktop-root: dry-run complete (target unchanged)"
else
  echo "provision-desktop-root: desktop payload staged at $rootfs"
  if [[ -n $selected_user ]]; then
    echo "provision-desktop-root: created target user $selected_user (password setup remains with the installer)"
  else
    echo "provision-desktop-root: user creation deferred; /etc/skel is ready"
  fi
fi
