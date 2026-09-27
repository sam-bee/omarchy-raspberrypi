#!/bin/bash

set -euo pipefail
source "$(dirname "$0")/base-test.sh"

require_command git
require_command realpath
require_command tar
bash -n "$ROOT/install/arm64/provision-desktop-root.sh"
bash -n "$ROOT/install/arm64/session/ensure-headless-output.sh"
pass "desktop provisioner and headless helper parse as shell"

test_tmp=$(mktemp -d)
trap 'rm -rf "$test_tmp"' EXIT
test_checkout="$test_tmp/source"
git clone -q --shared "$ROOT" "$test_checkout" || fail "clean source checkout is available"
[[ -z $(git -C "$test_checkout" status --porcelain --untracked-files=all) ]] || fail "source fixture is clean"

target="$test_tmp/root"
mkdir -p "$target/etc" "$target/usr" "$target/var"
printf 'root:x:0:0:root:/root:/bin/bash\n' >"$target/etc/passwd"
printf 'root:x:0:\n' >"$target/etc/group"

dry_run_output=$(bash "$ROOT/install/arm64/provision-desktop-root.sh" \
  --rootfs "$target" --source-checkout "$test_checkout" --dry-run)
grep -Fq 'dry-run complete (target unchanged)' <<<"$dry_run_output" || fail "generic dry-run completes"
[[ ! -e $target/usr/share/omarchy-pi ]] || fail "generic dry-run does not populate the target"
[[ ! -e $target/etc/skel ]] || fail "generic dry-run does not create skeleton files"
pass "generic root provisioning dry-run is non-mutating"

user_dry_run_output=$(bash "$ROOT/install/arm64/provision-desktop-root.sh" \
  --rootfs "$target" --source-checkout "$test_checkout" --user desktop --home /home/desktop --dry-run)
grep -Fq 'create target account desktop' <<<"$user_dry_run_output" || fail "user dry-run plans target account creation"
grep -Fq 'copy Omarchy release' <<<"$user_dry_run_output" || fail "user dry-run plans a versioned release"
[[ ! -e $target/home ]] || fail "user dry-run does not create a home"
pass "user provisioning dry-run keeps account creation target-local"

source_text=$(<"$ROOT/config/hypr/hyprland.lua")
grep -Fq 'require("hypr.monitors")' <<<"$source_text" || fail "fresh config uses upstream Hyprland modules"
if grep -Fq 'OMARCHY_PI_MINIMAL_SESSION' "$ROOT/install/arm64/provision-desktop-root.sh"; then
  fail "fresh provisioner must not force the reduced session profile"
fi
pass "fresh provisioning keeps upstream menu and binding defaults"
