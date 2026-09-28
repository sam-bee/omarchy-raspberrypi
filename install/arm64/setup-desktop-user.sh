#!/bin/bash

# Prepare the target user's Pi desktop from the staged Omarchy release.
# Run this once the release pointer and desktop packages are ready.
set -euo pipefail

usage() {
  echo "Usage: $0" >&2
  exit 2
}

die() {
  echo "Error: $*" >&2
  exit 1
}

(( $# == 0 )) || usage
(( EUID != 0 )) || die "run setup-desktop-user.sh as the target user, not as root"

[[ -n ${HOME:-} && $HOME == /* && -d $HOME && ! -L $HOME ]] ||
  die "HOME must be an absolute, real directory"

pi_current="$HOME/.local/share/omarchy-pi/current"
[[ -L $pi_current && -d $pi_current ]] ||
  die "the Pi current release symlink is missing or invalid: $pi_current"

export OMARCHY_PATH="$pi_current"

prepend_path() {
  case ":${PATH:-}:" in
    *":$1:"*) ;;
    *) PATH="$1${PATH:+:$PATH}" ;;
  esac
}

# Keep the staged release ahead of the user-owned tools, followed by mise's
# shims. This is the environment consumed by every leaf below.
prepend_path "$HOME/.local/share/mise/shims"
prepend_path "$HOME/.local/bin"
prepend_path "$OMARCHY_PATH/bin"
export PATH

for setup_leaf in \
  setup-desktop-theme.sh \
  setup-desktop-images.sh \
  setup-mise.sh \
  setup-user-agents.sh; do
  setup_path="$OMARCHY_PATH/install/arm64/$setup_leaf"
  [[ -f $setup_path && ! -L $setup_path ]] ||
    die "missing desktop setup helper: $setup_path"
done

echo "Preparing Omarchy desktop theme..."
bash "$OMARCHY_PATH/install/arm64/setup-desktop-theme.sh"

echo "Preparing Omarchy image associations..."
bash "$OMARCHY_PATH/install/arm64/setup-desktop-images.sh"

echo "Preparing mise..."
bash "$OMARCHY_PATH/install/arm64/setup-mise.sh"

echo "Preparing user agents and skills..."
bash "$OMARCHY_PATH/install/arm64/setup-user-agents.sh"

echo "Omarchy Pi desktop user setup complete; no default agent was selected."
