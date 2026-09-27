#!/bin/bash

set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

test_tmp=$(mktemp -d)
trap 'rm -rf "$test_tmp"' EXIT

stub_bin="$test_tmp/bin"
mkdir -p "$stub_bin"

# Keep the fallback path deterministic and record the descriptor slurp receives.
cat >"$stub_bin/omarchy-cmd-present" <<'SH'
#!/bin/bash
exit 1
SH

cat >"$stub_bin/slurp" <<'SH'
#!/bin/bash
readlink "/proc/$$/fd/0" >"$CAPTURE_REGION_STDIN_LOG"
SH
chmod +x "$stub_bin/omarchy-cmd-present" "$stub_bin/slurp"

# A caller such as SSH can leave an open pipe on stdin. Freeform selection
# must override it with /dev/null so slurp can map its layer immediately.
printf 'inherited input\n' |
  CAPTURE_REGION_STDIN_LOG="$test_tmp/stdin" PATH="$stub_bin:$ROOT/bin:/usr/bin:/bin" \
    "$ROOT/bin/omarchy-capture-region" region >/dev/null 2>&1 || true

[[ $(<"$test_tmp/stdin") == /dev/null ]] ||
  fail "freeform capture closes inherited stdin before launching slurp"
pass "freeform capture closes inherited stdin before launching slurp"

bash -n "$ROOT/bin/omarchy-capture-region" ||
  fail "capture-region helper remains syntactically valid"
pass "capture-region helper remains syntactically valid"
