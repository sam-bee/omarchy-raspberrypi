#!/bin/bash

set -euo pipefail

source "$(dirname "$0")/base-test.sh"

test_tmp=$(mktemp -d)
trap '/usr/bin/rm -rf "$test_tmp"' EXIT

stub_bin="$test_tmp/bin"
fake_root="$test_tmp/omarchy"
fake_home="$test_tmp/home"
fake_state="$test_tmp/migration-state"
mutation_log="$test_tmp/mutations"
mkdir -p "$stub_bin" "$fake_root/migrations" "$fake_home"

cat >"$stub_bin/uname" <<'STUB'
#!/bin/bash
printf '%s\n' "${TEST_UNAME:-aarch64}"
STUB
chmod +x "$stub_bin/uname"

# If a guarded command reaches one of these operations, the test records it and
# fails below. The command stubs deliberately do not emulate any system state.
for command in sudo pacman pacman-key systemctl limine-update limine-mkinitcpio limine-snapper-sync limine-snapper-restore mkinitcpio cryptsetup btrfs mount umount systemd-run script git; do
  cat >"$stub_bin/$command" <<'STUB'
#!/bin/bash
printf '%s\n' "${0##*/}" >>"$MUTATION_LOG"
exit 99
STUB
  chmod +x "$stub_bin/$command"
done

cat >"$fake_root/migrations/100-pending.sh" <<'MIGRATION'
echo should-not-run >>"$MUTATION_LOG"
MIGRATION

guarded_commands=(
  omarchy-apply-system
  omarchy-apply-hardware
  omarchy-update
  omarchy-update-system-pkgs
  omarchy-update-pacman
  omarchy-update-keyring
  omarchy-refresh-pacman
  omarchy-channel-set
  omarchy-reinstall-pkgs
  omarchy-refresh-limine
  omarchy-refresh-plymouth
  omarchy-plymouth-set
  omarchy-reinstall-configs
  omarchy-setup-direct-boot
  omarchy-hibernation-setup
  omarchy-hibernation-remove
  omarchy-upgrade-to-quattro
  omarchy-system-factory-reset
  omarchy-system-factory-reset-finish
  omarchy-provision-owner
  omarchy-snapshot
  omarchy-migrate
)

run_command() {
  local architecture="$1" command="$2"
  shift 2

  : >"$mutation_log"
  set +e
  TEST_UNAME="$architecture" \
    MUTATION_LOG="$mutation_log" \
    HOME="$fake_home" \
    OMARCHY_PATH="$fake_root" \
    OMARCHY_MIGRATION_STATE="$fake_state" \
    PATH="$stub_bin:$PATH" \
      "$ROOT/bin/$command" "$@" >"$test_tmp/stdout" 2>"$test_tmp/stderr"
  local status=$?
  set -e
  return "$status"
}

assert_no_local_state() {
  [[ -z "$(find "$fake_home" -mindepth 1 -print -quit)" ]] ||
    fail "$1 leaves files in the fake home" "$(find "$fake_home" -mindepth 1 -print)"
  [[ ! -e $fake_state ]] ||
    fail "$1 creates migration state on the guarded path" "$(find "$fake_state" -print)"
}

for architecture in aarch64 arm64 armv7l; do
  for command in "${guarded_commands[@]}"; do
    if [[ $command == "omarchy-migrate" ]]; then
      if run_command "$architecture" "$command"; then
        fail "$command refuses normal migration execution on $architecture"
      fi
    else
      if run_command "$architecture" "$command" --help; then
        fail "$command refuses execution on $architecture"
      fi
    fi

    grep -qF 'This command is not supported on ARM or other non-x86 platforms; use omarchy-install-plan for the ARM64 plan-only preview.' "$test_tmp/stderr" ||
      fail "$command explains the ARM64 plan-only path on $architecture" "$(cat "$test_tmp/stderr")"
    [[ ! -s $mutation_log ]] ||
      fail "$command reaches no mutation helper on $architecture" "$(cat "$mutation_log")"
    assert_no_local_state "$command on $architecture"
  done
  pass "native Omarchy entrypoints refuse $architecture before system mutation"
done

# Pending migration inspection is intentionally read-only and remains available
# on ARM so the plan and later deployment review can see migration state.
for mode in --pending --check; do
  if ! run_command aarch64 omarchy-migrate "$mode"; then
    fail "omarchy-migrate $mode remains available on ARM"
  fi
  grep -qxF '100-pending.sh' "$test_tmp/stdout" ||
    fail "omarchy-migrate $mode lists pending migrations on ARM" "$(cat "$test_tmp/stdout")"
  [[ ! -s $mutation_log ]] || fail "omarchy-migrate $mode performs no migration mutation"
  assert_no_local_state "omarchy-migrate $mode on ARM"
done
pass "omarchy-migrate pending inspection remains read-only on ARM"

# Exercise only x86 help paths that parse and exit before any privileged work.
x86_help_commands=(
  "omarchy-apply-system --help"
  "omarchy-apply-hardware --help"
  "omarchy-upgrade-to-quattro --help"
  "omarchy-migrate --help"
)
for invocation in "${x86_help_commands[@]}"; do
  read -r command argument <<<"$invocation"
  if ! run_command x86_64 "$command" "$argument"; then
    fail "$command still accepts its help path on x86_64" "$(cat "$test_tmp/stderr")"
  fi
  ! grep -qF 'This command is not supported on ARM or other non-x86 platforms' "$test_tmp/stderr" ||
    fail "$command does not reject x86_64 as non-x86"
  [[ ! -s $mutation_log ]] || fail "$command help path performs no mutation" "$(cat "$mutation_log")"
done
pass "guarded help paths remain available on x86_64"
