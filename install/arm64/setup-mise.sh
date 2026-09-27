#!/bin/bash

# ALARM does not currently package mise. Bootstrap the official ARM64 release
# per user, then let the regular Omarchy mise launchers manage developer tools.
set -euo pipefail

if (( EUID == 0 )); then
  echo "Run as the target user without sudo" >&2
  exit 2
fi
[[ $(uname -sm) == "Linux aarch64" ]] || {
  echo "This bootstrap requires Linux aarch64" >&2
  exit 2
}

if command -v mise >/dev/null 2>&1; then
  mise --version
  exit 0
fi
target="$HOME/.local/bin/mise"
if [[ -x $target ]]; then
  "$target" --version
  exit 0
elif [[ -e $target || -L $target ]]; then
  echo "Refusing to replace an existing non-executable mise: $target" >&2
  exit 1
fi

# SHA-256 from this release's official SHASUMS256.txt. Keep the release and
# digest together when updating the bootstrap; existing installs are retained.
version=2026.9.14
digest=b405a2ea062c4d9560eee0a3c45b79e53c8834cf38e956bb47ca70e0d2e7479a
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
curl --fail --location --retry 3 --connect-timeout 15 --max-time 300 \
  "https://github.com/jdx/mise/releases/download/v$version/mise-v$version-linux-arm64" \
  --output "$scratch/mise"
printf '%s  %s\n' "$digest" "$scratch/mise" | sha256sum --check --status
chmod 755 "$scratch/mise"
"$scratch/mise" --version
mkdir -p "$HOME/.local/bin"
install -m755 "$scratch/mise" "$target"
echo "mise installed at $target; update it with mise self-update"
