#!/bin/bash

set -euo pipefail

source "$(dirname "$0")/base-test.sh"

require_command python3
python3 "$ROOT/install/arm64/installer-image/test-recovery.py" || fail "USB recovery contract tests pass"
pass "USB recovery contract tests pass"
