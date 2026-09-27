#!/bin/bash

# Prepare the first bounded Pi desktop theme without starting shell services.
# Run as the target user after the minimal session release has been staged.
set -euo pipefail

usage() {
  echo "Usage: $0 [theme-name]" >&2
  exit 2
}

if (( $# > 1 )); then
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

theme_name=${1:-Tokyo Night}
config_dir="$HOME/.config"
state_dir="$HOME/.local/state/omarchy/current"
theme_name_path="$state_dir/theme.name"
theme_dir="$state_dir/theme"
background_link="$state_dir/background"
foot_dir="$config_dir/foot"
foot_config="$foot_dir/foot.ini"
active_theme=""

: "${OMARCHY_PATH:?OMARCHY_PATH must point at the staged Omarchy release}"
[[ $OMARCHY_PATH == /* && -d $OMARCHY_PATH ]] || {
  echo "OMARCHY_PATH must point at a staged release" >&2
  exit 1
}
theme_set="$OMARCHY_PATH/bin/omarchy-theme-set"
foot_template="$OMARCHY_PATH/config/foot/foot.ini"
[[ -x $theme_set ]] || { echo "Missing theme renderer: $theme_set" >&2; exit 1; }
[[ -f $foot_template && ! -L $foot_template ]] || {
  echo "Missing Foot template: $foot_template" >&2
  exit 1
}
export PATH="$OMARCHY_PATH/bin:$PATH"

path_exists() {
  [[ -e $1 || -L $1 ]]
}

if path_exists "$theme_name_path"; then
  [[ -f $theme_name_path && ! -L $theme_name_path && -s $theme_name_path ]] || {
    echo "Refusing an invalid existing theme marker: $theme_name_path" >&2
    exit 1
  }
  [[ -d $theme_dir && ! -L $theme_dir ]] || {
    echo "Existing theme marker has no rendered theme directory: $theme_dir" >&2
    exit 1
  }
  active_theme=$(<"$theme_name_path")
  echo "Preserving existing Omarchy theme: $active_theme"
else
  # An unmarked state directory may be an interrupted or manually-created
  # setup. Do not let the default theme overwrite it.
  if path_exists "$state_dir"; then
    [[ -d $state_dir && ! -L $state_dir ]] || {
      echo "Refusing an invalid Omarchy theme state directory: $state_dir" >&2
      exit 1
    }
    if find "$state_dir" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
      echo "Refusing an unmarked non-empty Omarchy theme state: $state_dir" >&2
      exit 1
    fi
  fi

  # Headless mode still renders the theme files and background state, but skips
  # IPC and post-theme hooks that require a live desktop.
  OMARCHY_THEME_HEADLESS=1 "$theme_set" "$theme_name"
  active_theme=$(<"$theme_name_path")
fi

# Do not claim a prepared desktop when a renderer helper failed part-way
# through. These are the files consumed by the shell, Foot and the background
# layer respectively.
for rendered in colors.toml shell.toml foot.ini; do
  [[ -f $theme_dir/$rendered && ! -L $theme_dir/$rendered ]] || {
    echo "Theme rendering did not produce $theme_dir/$rendered" >&2
    exit 1
  }
done
[[ -L $background_link && -f $background_link ]] || {
  echo "Theme rendering did not select a background: $background_link" >&2
  exit 1
}

if path_exists "$foot_config"; then
  echo "Preserving existing Foot configuration: $foot_config"
  exit 0
fi

mkdir -p -- "$foot_dir"
tmp=$(mktemp "$foot_dir/.foot.ini.XXXXXXXX")
cleanup() {
  rm -f -- "$tmp"
}
trap cleanup EXIT

install -m 644 -- "$foot_template" "$tmp"

# The package normally supplies JetBrainsMono Nerd Font. Foot accepts the
# generic family name when that font is unavailable on a minimal Pi image.
if ! command -v fc-list >/dev/null 2>&1 || ! fc-list : family 2>/dev/null | grep -Fqi 'JetBrainsMono Nerd Font'; then
  sed -i -E 's/^font=JetBrainsMono Nerd Font(:.*)?$/font=monospace\1/' "$tmp"
fi

# A hard link is an atomic create in the same directory and cannot overwrite a
# file or symlink created by another writer after the initial preflight.
if ! ln -T -- "$tmp" "$foot_config"; then
  if path_exists "$foot_config"; then
    echo "Preserving Foot configuration created by another writer: $foot_config"
    exit 0
  fi
  echo "Could not publish Foot configuration: $foot_config" >&2
  exit 1
fi
rm -f -- "$tmp"
trap - EXIT
echo "Prepared Omarchy theme and Foot configuration for ${active_theme}."
