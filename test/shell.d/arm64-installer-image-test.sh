#!/bin/bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"
require_command python3
require_command zstd
for name in desktop-payload build-installer-image stage-installer-services provision-access installer-session verify-installer-image stage-arm-packages; do
  python3 "$ROOT/install/arm64/installer-image/test-$name.py"
done
