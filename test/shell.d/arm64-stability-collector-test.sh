#!/bin/bash
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

collector="$ROOT/docs/deployment/maintenance/collect-pi-stability.sh"
temp_dir=$(mktemp -d)
trap 'rm -rf -- "$temp_dir"' EXIT

make_minimal_path() {
  local destination=$1
  mkdir -p -- "$destination"
  local utility
  for utility in awk cat chmod date grep id mkdir rm sed sort timeout tr uname wc; do
    local resolved
    resolved=$(command -v "$utility") || fail "test host is missing $utility"
    ln -s -- "$resolved" "$destination/$utility"
  done
}

assert_mode() {
  local expected=$1
  local path=$2
  local actual
  actual=$(stat -c '%a' -- "$path")
  [[ $actual == "$expected" ]] || fail "$path has mode $actual, expected $expected"
}

assert_status() {
  local status_file=$1
  local check=$2
  local result=$3
  awk -F '\t' -v check="$check" -v result="$result" '$1 == check && $2 == result { found = 1 } END { exit !found }' "$status_file" || fail "$check did not have status $result"
}

minimal_path="$temp_dir/minimal-bin"
make_minimal_path "$minimal_path"

skip_output="$temp_dir/skip-output"
PATH="$minimal_path" "$collector" "$skip_output"
assert_mode 700 "$skip_output"
assert_mode 600 "$skip_output/status.tsv"
assert_status "$skip_output/status.tsv" 'package-version:sqlite' SKIP
assert_status "$skip_output/status.tsv" raspberry-pi-thermal SKIP
assert_status "$skip_output/status.tsv" current-boot-kernel-events SKIP

fail_path="$temp_dir/fail-bin"
make_minimal_path "$fail_path"
cat > "$fail_path/pacman" <<'EOF'
#!/bin/bash
if [[ $1 == '-Q' && $2 == 'sqlite' ]]; then
  printf 'fixture: injected local package query failure\n' >&2
  exit 42
fi
if [[ $1 == '-Q' ]]; then
  printf '%s 1.0\n' "$2"
  exit 0
fi
if [[ $1 == '-Qkk' ]]; then
  printf '%s: 0 altered files\n' "$2"
  exit 0
fi
exit 64
EOF
chmod 755 -- "$fail_path/pacman"

fail_output="$temp_dir/fail-output"
collector_rc=0
PATH="$fail_path" "$collector" "$fail_output" || collector_rc=$?
[[ $collector_rc == 1 ]] || fail "a failed package query returned $collector_rc, expected 1"
assert_mode 700 "$fail_output"
assert_mode 600 "$fail_output/package-sqlite.txt"
assert_status "$fail_output/status.tsv" 'package-version:sqlite' FAIL
assert_status "$fail_output/status.tsv" 'package-integrity:sqlite' SKIP

empty_path="$temp_dir/empty-bin"
make_minimal_path "$empty_path"
cat > "$empty_path/pacman" <<'EOF'
#!/bin/bash
if [[ $1 == '-Q' ]]; then
  printf '%s 1.0\n' "$2"
  exit 0
fi
if [[ $1 == '-Qkk' ]]; then
  if [[ $2 == 'qt6-declarative' && ${PACMAN_CHECKSUM_FIXTURE:-} == rc0 ]]; then
    printf 'warning: qt6-declarative: /usr/lib/libExample.so (SHA256 checksum mismatch)\n'
    exit 0
  fi
  if [[ $2 == 'qt6-declarative' && ${PACMAN_CHECKSUM_FIXTURE:-} == rc1 ]]; then
    printf 'warning: qt6-declarative: /usr/lib/libExample.so (SHA256 checksum mismatch)\n'
    exit 1
  fi
  printf '%s: 0 altered files\n' "$2"
  exit 0
fi
exit 64
EOF
cat > "$empty_path/systemctl" <<'EOF'
#!/bin/bash
case " $* " in
  *' --plain '* ) ;;
  * ) printf 'systemctl was called without --plain\n' >&2; exit 90 ;;
esac
case " $* " in
  *' list-units '*) printf 'example.service loaded active running Example service\n' ;;
esac
EOF
cat > "$empty_path/loginctl" <<'EOF'
#!/bin/bash
exit 0
EOF
cat > "$empty_path/journalctl" <<'EOF'
#!/bin/bash
if [[ ${JOURNAL_FIXTURE:-} == events ]]; then
  printf '[ 2.000] nvme nvme0: error Authorization: Bearer bearer-secret password=password-secret\n'
  exit 0
fi
if [[ ${JOURNAL_FIXTURE:-} == timeout ]]; then
  printf '%s\n' '-- No entries --'
  exit 124
fi
printf '%s\n' '-- No entries --'
exit 1
EOF
cat > "$empty_path/coredumpctl" <<'EOF'
#!/bin/bash
expected=(--no-pager --no-legend -n 200 list "_BOOT_ID=${EXPECTED_BOOT_ID:-invalid}")
actual=("$@")
if (( $# != ${#expected[@]} )); then
  printf 'unexpected coredumpctl argument count\n' >&2
  exit 90
fi
for index in "${!expected[@]}"; do
  if [[ ${actual[$index]} != "${expected[$index]}" ]]; then
    printf 'unsupported coredumpctl argument: %s\n' "${actual[$index]}" >&2
    exit 91
  fi
done
printf 'No coredumps found.\n'
exit 1
EOF
chmod 755 -- "$empty_path/pacman" "$empty_path/systemctl" "$empty_path/loginctl" "$empty_path/journalctl" "$empty_path/coredumpctl"

boot_id=$(</proc/sys/kernel/random/boot_id)
expected_boot_id=${boot_id//-/}
empty_output="$temp_dir/empty-output"
EXPECTED_BOOT_ID="$expected_boot_id" PATH="$empty_path" "$collector" "$empty_output"
assert_status "$empty_output/status.tsv" system-failed-units PASS
assert_status "$empty_output/status.tsv" user-service-units SKIP
assert_status "$empty_output/status.tsv" current-boot-kernel-events PASS
assert_status "$empty_output/status.tsv" current-boot-coredumps PASS
assert_status "$empty_output/status.tsv" 'package-integrity:qt6-base' PASS
grep -Fxq $'example.service\tloaded\tactive\trunning' "$empty_output/system-service-units.tsv" || fail 'systemctl unit columns were shifted'

redact_output="$temp_dir/redact-output"
EXPECTED_BOOT_ID="$expected_boot_id" JOURNAL_FIXTURE=events PATH="$empty_path" "$collector" "$redact_output"
assert_status "$redact_output/status.tsv" current-boot-kernel-events WARN
grep -Fq '[REDACTED]' "$redact_output/current-boot-kernel-events.txt" || fail 'journal credentials were not redacted'
if grep -Eq 'bearer-secret|password-secret' "$redact_output/current-boot-kernel-events.txt"; then
  fail 'journal fixture leaked a credential value'
fi

timeout_output="$temp_dir/timeout-output"
timeout_rc=0
EXPECTED_BOOT_ID="$expected_boot_id" JOURNAL_FIXTURE=timeout PATH="$empty_path" "$collector" "$timeout_output" || timeout_rc=$?
[[ $timeout_rc == 1 ]] || fail "journal timeout fixture returned $timeout_rc, expected 1"
assert_status "$timeout_output/status.tsv" current-boot-kernel-events FAIL

checksum_rc0_output="$temp_dir/checksum-rc0-output"
EXPECTED_BOOT_ID="$expected_boot_id" PACMAN_CHECKSUM_FIXTURE=rc0 PATH="$empty_path" "$collector" "$checksum_rc0_output"
assert_status "$checksum_rc0_output/status.tsv" 'package-integrity:qt6-declarative' WARN

checksum_rc1_output="$temp_dir/checksum-rc1-output"
EXPECTED_BOOT_ID="$expected_boot_id" PACMAN_CHECKSUM_FIXTURE=rc1 PATH="$empty_path" "$collector" "$checksum_rc1_output"
assert_status "$checksum_rc1_output/status.tsv" 'package-integrity:qt6-declarative' WARN

pass 'stability collector records unavailable, failed, filtered, and private evidence'
