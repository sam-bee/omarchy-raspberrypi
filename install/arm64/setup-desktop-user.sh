#!/bin/bash

# Prepare the target user's Pi desktop from the staged Omarchy runtime.
# Run this once the desktop packages are ready. Legacy source-release targets
# remain supported; packaged targets use the system Omarchy path directly.
set -euo pipefail

usage() {
  echo "Usage: $0 [--runtime-layout legacy|packaged]" >&2
  exit 2
}

die() {
  echo "Error: $*" >&2
  exit 1
}

runtime_mode=${OMARCHY_PI_RUNTIME_MODE:-legacy}
while (( $# )); do
  case "$1" in
    --runtime-mode|--runtime-layout)
      (( $# >= 2 )) || usage
      runtime_mode=$2
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      usage
      ;;
  esac
done
case "$runtime_mode" in
  legacy|packaged) ;;
  *) die "unsupported runtime mode: $runtime_mode" ;;
esac
(( EUID != 0 )) || die "run setup-desktop-user.sh as the target user, not as root"

[[ -n ${HOME:-} && $HOME == /* && -d $HOME && ! -L $HOME ]] ||
  die "HOME must be an absolute, real directory"

if [[ $runtime_mode == packaged ]]; then
  [[ ${OMARCHY_PATH:-/usr/share/omarchy} == /usr/share/omarchy ]] ||
    die "packaged runtime requires OMARCHY_PATH=/usr/share/omarchy"
  [[ -d /usr/share/omarchy && ! -L /usr/share/omarchy ]] ||
    die "the packaged Omarchy runtime is missing or invalid: /usr/share/omarchy"
  export OMARCHY_PATH=/usr/share/omarchy
else
  pi_current="$HOME/.local/share/omarchy-pi/current"
  [[ -L $pi_current && -d $pi_current ]] ||
    die "the Pi current release symlink is missing or invalid: $pi_current"
  export OMARCHY_PATH="$pi_current"
fi
export OMARCHY_PI_RUNTIME_MODE="$runtime_mode"

prepend_path() {
  case ":${PATH:-}:" in
    *":$1:"*) ;;
    *) PATH="$1${PATH:+:$PATH}" ;;
  esac
}

# Keep user-owned tools available, followed by mise's shims. Packaged Omarchy
# commands are provided by /usr/bin; legacy commands remain in the release.
prepend_path "$HOME/.local/share/mise/shims"
prepend_path "$HOME/.local/bin"
if [[ $runtime_mode == legacy ]]; then
  prepend_path "$OMARCHY_PATH/bin"
fi
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
