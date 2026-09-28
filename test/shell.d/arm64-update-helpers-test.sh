#!/bin/bash

set -euo pipefail

source "$(dirname "$0")/base-test.sh"

require_command python3
python3 "$ROOT/install/arm64/test-update-helpers.py" || fail "ARM update helper tests pass"
pass "ARM update helper tests pass"
