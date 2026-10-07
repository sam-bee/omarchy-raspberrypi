#!/bin/bash

# Prepare the target user's image associations and the small imv integration
# used by the Pi desktop. Run this after the image viewer package and the
# desktop theme have been installed; it does not change system defaults.
set -euo pipefail

usage() {
  echo "Usage: $0" >&2
  exit 2
}

if (( $# != 0 )); then
  usage
fi
if (( EUID == 0 )); then
  echo "Run as the target user without sudo" >&2
  exit 2
fi
if [[ -z ${HOME:-} || $HOME != /* || ! -d $HOME || -L $HOME ]]; then
  echo "HOME must be an absolute, real directory" >&2
  exit 2
fi
if [[ -n ${XDG_CONFIG_HOME:-} && ${XDG_CONFIG_HOME%/} != "$HOME/.config" ]]; then
  echo "This Pi image workflow requires XDG_CONFIG_HOME to be unset or $HOME/.config" >&2
  exit 2
fi

: "${OMARCHY_PATH:?OMARCHY_PATH must point at the Omarchy runtime}"
[[ $OMARCHY_PATH == /* && -d $OMARCHY_PATH ]] || {
  echo "OMARCHY_PATH must point at an Omarchy runtime" >&2
  exit 1
}

config_home="$HOME/.config"

theme_state="$HOME/.local/state/omarchy/current"
theme_name_path="$theme_state/theme.name"
theme_backgrounds="$config_home/omarchy/backgrounds"
imv_config_dir="$config_home/imv"
imv_config="$imv_config_dir/config"
if [[ ${OMARCHY_PI_RUNTIME_MODE:-legacy} == packaged ]]; then
  theme_bg_set=/usr/bin/omarchy-theme-bg-set
else
  theme_bg_set="$OMARCHY_PATH/bin/omarchy-theme-bg-set"
fi

[[ -x $theme_bg_set ]] || {
  echo "Missing background helper: $theme_bg_set" >&2
  exit 1
}
command -v xdg-mime >/dev/null 2>&1 || {
  echo "Missing required command: xdg-mime" >&2
  exit 1
}
command -v imv >/dev/null 2>&1 || {
  echo "Missing required command: imv" >&2
  exit 1
}

desktop_found=0
desktop_home="${XDG_DATA_HOME:-$HOME/.local/share}/applications/imv.desktop"
for desktop_file in \
  "$desktop_home" \
  "$HOME/.local/share/omarchy/applications/imv.desktop" \
  "$OMARCHY_PATH/applications/imv.desktop" \
  /usr/local/share/applications/imv.desktop \
  /usr/share/applications/imv.desktop \
  /usr/share/omarchy/applications/imv.desktop; do
  if [[ -f $desktop_file && ! -L $desktop_file ]]; then
    desktop_found=1
    break
  fi
done
(( desktop_found )) || {
  echo "Missing imv desktop entry: install imv before running this helper" >&2
  exit 1
}

[[ -f $theme_name_path && ! -L $theme_name_path && -s $theme_name_path ]] || {
  echo "Missing active Omarchy theme: run setup-desktop-theme.sh first" >&2
  exit 1
}
theme_name=$(<"$theme_name_path")
[[ $theme_name != */* && $theme_name != .* ]] || {
  echo "Invalid active Omarchy theme name: $theme_name" >&2
  exit 1
}

# xdg-mime updates the user's XDG config file and leaves unrelated MIME
# defaults in place. Keep the list aligned with applications/imv.desktop so
# images opened from a file manager use the same viewer as explicit launches.
image_mime_types=(
  image/png
  image/jpeg
  image/jpg
  image/gif
  image/webp
  image/bmp
  image/tiff
  image/x-xcf
  image/x-portable-pixmap
  image/x-xbitmap
)
for mime_type in "${image_mime_types[@]}"; do
  xdg-mime default imv.desktop "$mime_type"
done

# The existing Omarchy imv config contains optional tools such as Omasnap,
# printing and image editing. On a fresh Pi profile, seed only the portable
# background action. A user-managed config is never replaced or amended.
if [[ -e $imv_config || -L $imv_config ]]; then
  echo "Preserving existing imv configuration: $imv_config"
else
  mkdir -p -- "$imv_config_dir"
  tmp=$(mktemp "$imv_config_dir/.config.XXXXXXXX")
  cleanup() {
    rm -f -- "$tmp"
  }
  trap cleanup EXIT
  cat >"$tmp" <<'EOF'
[binds]

# Set the current image as the Omarchy background.
<Ctrl+b> = exec omarchy-theme-bg-set "$imv_current_file" &
EOF
  chmod 644 "$tmp"
  if ! ln -T -- "$tmp" "$imv_config"; then
    if [[ -e $imv_config || -L $imv_config ]]; then
      echo "Preserving imv configuration created by another writer: $imv_config"
    else
      echo "Could not publish imv configuration: $imv_config" >&2
      exit 1
    fi
  fi
  rm -f -- "$tmp"
  trap - EXIT
fi

# This is the user directory consumed by Omarchy's background switcher.
# Creating it here makes the file-manager workflow usable immediately without
# changing the selected background.
mkdir -p -- "$theme_backgrounds/$theme_name"

echo "Prepared user image associations and imv background action."
