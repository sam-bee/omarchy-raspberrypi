#!/bin/bash

set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

require_command python3
module="$ROOT/install/arm64/installer-image/installed_target.py"
tests="$ROOT/install/arm64/installer-image/test-installed-target.py"
[[ -f $module ]] || fail "mounted-target provisioner exists"
[[ -f $tests ]] || fail "mounted-target provisioner tests exist"
python3 -m py_compile "$module" "$tests" || fail "mounted-target Python files compile"
python3 "$tests" || fail "mounted-target command-runner tests pass"

grep -Fq 'network-namespace-path=/proc/1/ns/net' "$module" || fail "target setup joins the installer network namespace"
grep -Fq 'resolv-conf=replace-host' "$module" || fail "target setup supplies the installer resolver"
grep -Fq '"--pipe"' "$module" || fail "target setup keeps nspawn stdin on a pipe"
grep -Fq 'bind=" + os.fspath(boot) + ":/boot' "$module" || fail "target setup binds mounted boot explicitly"
if grep -Eq 'systemctl[^\n]*(--now|[[:space:]]start([[:space:]]|$))' "$module"; then
  fail "target provisioning does not start target daemons"
fi

pass "mounted-target provisioning validates settings, secrets, target paths, and isolated setup"
