#!/bin/bash

# Set up the upstream Mise-backed agent tools and their user skill links on a
# Pi target. This is deliberately a user-owned leaf: it does not run the full
# Omarchy finalizer, select a default agent, or make system configuration changes.
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
(( EUID != 0 )) || die "run setup-user-agents.sh as the target user, not as root"

[[ -n ${HOME:-} && $HOME == /* && -d $HOME && ! -L $HOME ]] ||
  die "HOME must be an absolute, real directory"

if [[ $runtime_mode == packaged ]]; then
  [[ ${OMARCHY_PATH:-/usr/share/omarchy} == /usr/share/omarchy ]] ||
    die "packaged runtime requires OMARCHY_PATH=/usr/share/omarchy"
  [[ -d /usr/share/omarchy && ! -L /usr/share/omarchy ]] ||
    die "the packaged Omarchy runtime is missing or invalid: /usr/share/omarchy"
  export OMARCHY_PATH=/usr/share/omarchy
else
  pi_root="$HOME/.local/share/omarchy-pi"
  pi_current="$pi_root/current"
  if [[ -n ${OMARCHY_PATH:-} && ${OMARCHY_PATH%/} != "$pi_current" ]]; then
    die "OMARCHY_PATH must be the Pi current release symlink: $pi_current"
  fi
  [[ -L $pi_current && -d $pi_current ]] ||
    die "the Pi current release symlink is missing or invalid: $pi_current"
  export OMARCHY_PATH="$pi_current"
fi
export OMARCHY_PI_RUNTIME_MODE="$runtime_mode"
export OMARCHY_INSTALL="$OMARCHY_PATH/install"
export OMARCHY_SETUP_CONTEXT=runtime
if [[ $runtime_mode == legacy ]]; then
  case ":${PATH:-}:" in
    *":$OMARCHY_PATH/bin:"*) ;;
    *) PATH="$OMARCHY_PATH/bin${PATH:+:$PATH}" ;;
  esac
fi
case ":${PATH:-}:" in
  *":$HOME/.local/share/mise/shims:"*) ;;
  *) PATH="${PATH:+$PATH:}$HOME/.local/share/mise/shims" ;;
esac
case ":${PATH:-}:" in
  *":$HOME/.local/bin:"*) ;;
  *) PATH="${PATH:+$PATH:}$HOME/.local/bin" ;;
esac
export PATH

command -v -- mise >/dev/null 2>&1 ||
  die "mise is required; install it before running setup-user-agents.sh"

for setup_leaf in \
  "$OMARCHY_INSTALL/user/mise-work.sh" \
  "$OMARCHY_INSTALL/user/mise.sh" \
  "$OMARCHY_INSTALL/user/agent-skills.sh"; do
  [[ -f $setup_leaf && ! -L $setup_leaf ]] || die "missing setup leaf: $setup_leaf"
done

# Keep the upstream package list and Node setup as the source of truth. These
# leaves only see the target user's HOME and never invoke the full provisioner.
source "$OMARCHY_INSTALL/user/mise-work.sh"
source "$OMARCHY_INSTALL/user/mise.sh"
source "$OMARCHY_INSTALL/user/agent-skills.sh"

fragment_dir="$HOME/.config/omarchy"
fragment="$fragment_dir/pi-agent-shell.sh"
fragment_marker="# Omarchy Pi agent shell setup (managed)"
mkdir -p -- "$fragment_dir"

if [[ -L $fragment || -e $fragment ]]; then
  [[ -f $fragment && ! -L $fragment ]] || die "refusing a non-file shell fragment: $fragment"
  grep -Fqx "$fragment_marker" "$fragment" ||
    die "refusing to overwrite an existing shell fragment: $fragment"
else
  fragment_tmp=$(mktemp "$fragment_dir/.pi-agent-shell.XXXXXXXX")
  cleanup_fragment_tmp() {
    rm -f -- "$fragment_tmp"
  }
  trap cleanup_fragment_tmp EXIT
  if [[ $runtime_mode == packaged ]]; then
    cat >"$fragment_tmp" <<'EOF'
# Omarchy Pi agent shell setup (managed)
# Keep this fragment narrow: the full desktop bash setup is not available on
# every Pi image, while these paths are needed by login and interactive shells.
export OMARCHY_PATH="/usr/share/omarchy"
export OMARCHY_PI_RUNTIME_MODE=packaged

case ":${PATH:-}:" in
  *":$HOME/.local/share/mise/shims:"*) ;;
  *) PATH="${PATH:+$PATH:}$HOME/.local/share/mise/shims" ;;
esac
case ":${PATH:-}:" in
  *":$HOME/.local/bin:"*) ;;
  *) PATH="${PATH:+$PATH:}$HOME/.local/bin" ;;
esac
export PATH

if [[ $- == *i* ]] && command -v mise >/dev/null 2>&1; then
  eval "$(mise activate bash)"
  alias a='omarchy-agent --inline'
  alias c='opencode --auto'
  alias cx='printf "\033[2J\033[3J\033[H" && claude --permission-mode auto'
  alias cy='codex --approve-for-me'
fi
EOF
  else
    cat >"$fragment_tmp" <<'EOF'
# Omarchy Pi agent shell setup (managed)
# Keep this fragment narrow: the full desktop bash setup is not available on
# every Pi image, while these paths are needed by login and interactive shells.
export OMARCHY_PATH="$HOME/.local/share/omarchy-pi/current"

case ":${PATH:-}:" in
  *":$OMARCHY_PATH/bin:"*) ;;
  *) PATH="$OMARCHY_PATH/bin${PATH:+:$PATH}" ;;
esac
case ":${PATH:-}:" in
  *":$HOME/.local/share/mise/shims:"*) ;;
  *) PATH="${PATH:+$PATH:}$HOME/.local/share/mise/shims" ;;
esac
case ":${PATH:-}:" in
  *":$HOME/.local/bin:"*) ;;
  *) PATH="${PATH:+$PATH:}$HOME/.local/bin" ;;
esac
export PATH

if [[ $- == *i* ]] && command -v mise >/dev/null 2>&1; then
  eval "$(mise activate bash)"
  alias a='omarchy-agent --inline'
  alias c='opencode --auto'
  alias cx='printf "\033[2J\033[3J\033[H" && claude --permission-mode auto'
  alias cy='codex --approve-for-me'
fi
EOF
  fi
  chmod 644 -- "$fragment_tmp"
  mv -T -- "$fragment_tmp" "$fragment"
  trap - EXIT
fi

source_begin="# >>> omarchy-pi-agent-shell >>>"
source_line='source "$HOME/.config/omarchy/pi-agent-shell.sh"'
source_end="# <<< omarchy-pi-agent-shell <<<"

path_exists() {
  [[ -e $1 || -L $1 ]]
}

managed_source_block() {
  printf '%s\n%s\n%s\n' "$source_begin" "$source_line" "$source_end"
}

backup_before_edit() {
  local file=$1 backup="$1.omarchy-pi-agent.bak"
  if [[ -e $backup || -L $backup ]]; then
    return 0
  fi
  cp -p -- "$file" "$backup"
}

prepend_source_block() {
  local file=$1 tmp
  if path_exists "$file"; then
    [[ -f $file && ! -L $file ]] || die "refusing to edit non-file shell startup path: $file"
    grep -Fqx "$source_begin" "$file" && return 0
    backup_before_edit "$file"
  fi

  tmp=$(mktemp "$(dirname -- "$file")/.$(basename -- "$file").XXXXXXXX")
  {
    managed_source_block
    if [[ -f $file ]]; then
      cat -- "$file"
    fi
  } >"$tmp"
  if [[ -f $file ]]; then
    chmod --reference="$file" "$tmp"
  else
    chmod 644 -- "$tmp"
  fi
  mv -T -- "$tmp" "$file"
}

append_source_block() {
  local file=$1 tmp
  if path_exists "$file"; then
    [[ -f $file && ! -L $file ]] || die "refusing to edit non-file shell startup path: $file"
    grep -Fqx "$source_begin" "$file" && return 0
    backup_before_edit "$file"
  fi

  tmp=$(mktemp "$(dirname -- "$file")/.$(basename -- "$file").XXXXXXXX")
  if [[ -f $file ]]; then
    cat -- "$file" >"$tmp"
    printf '\n' >>"$tmp"
  fi
  managed_source_block >>"$tmp"
  if [[ -f $file ]]; then
    chmod --reference="$file" "$tmp"
  else
    chmod 644 -- "$tmp"
  fi
  mv -T -- "$tmp" "$file"
}

bashrc="$HOME/.bashrc"
bash_profile="$HOME/.bash_profile"

# bashrc's stock Arch guard returns before the rest of the file for `bash -lc`,
# so the managed source must be before that guard. The original file is copied
# once beside itself before this insertion and is otherwise retained byte for
# byte.
prepend_source_block "$bashrc"

# A normal Arch profile already sources bashrc. If a user profile does not,
# append the managed source so login shells still receive the Pi paths without
# replacing the profile's custom commands.
if path_exists "$bash_profile" &&
  grep -Eq '(^|[[:space:];])([.]|source)[[:space:]]+[^#]*bashrc' "$bash_profile"; then
  :
else
  append_source_block "$bash_profile"
fi

echo "Omarchy Pi agent setup complete; no default agent was selected."
