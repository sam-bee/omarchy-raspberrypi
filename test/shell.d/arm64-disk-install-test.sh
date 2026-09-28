#!/bin/bash

set -euo pipefail

source "$(dirname "$0")/base-test.sh"

require_command python3
python3 "$ROOT/install/arm64/installer-image/test-disk-install.py"
pass "ARM64 disk installer storage guards and command boundaries"
