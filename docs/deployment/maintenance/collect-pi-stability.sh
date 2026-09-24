#!/bin/bash

# Read-only, bounded stability evidence collection for the Raspberry Pi.
set -u
umask 077
export LC_ALL=C

readonly TIMEOUT_SECONDS=15
readonly PACKAGE_TIMEOUT_SECONDS=10
readonly PACKAGES=(sqlite util-linux util-linux-libs pam openssh glslang qt6-base qt6-declarative glibc quickshell)
readonly JOURNAL_PATTERN='segfault|general protection|oops|kernel panic|watchdog|soft lockup|hard lockup|out of memory|oom-kill|nvme|i/o error|blk_update_request|buffer i/o|ext4.*(error|warning)|fat-fs|fat.*(dirty|unclean)|unclean.*(shutdown|filesystem|journal)|filesystem.*(error|dirty|unclean)|f2fs.*(error|warning)|btrfs.*(error|warning)|ata.*(error|failed|timeout|reset)|pcie.*(error|fatal|timeout)|mmc.*(error|timeout)|uas.*(error|reset)|usb.*(error|reset|disconnect)|throttl|under.?voltage|voltage'

usage() {
  cat <<'EOF'
Usage: sudo bash collect-pi-stability.sh [NEW_OUTPUT_DIRECTORY]

Collects bounded, read-only Raspberry Pi stability evidence. If omitted, a
timestamped private directory is created under /root (or $HOME when not root).
The output directory must not already exist.
EOF
}

if (( $# > 1 )); then
  usage >&2
  exit 64
fi

if (( $# == 1 )); then
  output_dir=$1
else
  timestamp=$(date -u +%Y%m%dT%H%M%SZ 2>/dev/null || printf 'unknown-time')
  if (( EUID == 0 )); then
    output_base=/root
  else
    output_base=${HOME:-/tmp}
  fi
  output_dir="$output_base/pi-stability-$timestamp-$$"
fi

if ! mkdir -m 700 -- "$output_dir" 2>/dev/null; then
  printf 'Cannot create new output directory: %s\n' "$output_dir" >&2
  exit 73
fi
if ! chmod 700 -- "$output_dir"; then
  printf 'Cannot make output directory private: %s\n' "$output_dir" >&2
  exit 73
fi

readonly OUTPUT_DIR=$output_dir
readonly STATUS_FILE="$OUTPUT_DIR/status.tsv"
fail_count=0
warn_count=0
skip_count=0
pass_count=0
boot_id=unavailable
boot_id_match=''
boot_id_valid=0

: > "$STATUS_FILE"
chmod 600 -- "$STATUS_FILE"
printf 'check\tresult\tartifact\tdetail\n' >> "$STATUS_FILE"

if [[ -r /proc/sys/kernel/random/boot_id ]]; then
  IFS= read -r boot_id < /proc/sys/kernel/random/boot_id || true
fi
if [[ $boot_id =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]]; then
  boot_id_match=${boot_id//-/}
  boot_id_match=${boot_id_match,,}
  if [[ $boot_id_match =~ ^[[:xdigit:]]{32}$ ]]; then
    boot_id_valid=1
  else
    boot_id=unavailable
    boot_id_match=''
  fi
else
  boot_id=unavailable
fi

init_artifact() {
  local artifact=$1
  : > "$OUTPUT_DIR/$artifact"
  chmod 600 -- "$OUTPUT_DIR/$artifact"
}

record() {
  local check=$1
  local result=$2
  local artifact=$3
  local detail=$4
  detail=${detail//$'\t'/ }
  detail=${detail//$'\n'/ }
  detail=${detail//$'\r'/ }
  printf '%s\t%s\t%s\t%s\n' "$check" "$result" "$artifact" "$detail" >> "$STATUS_FILE"
  case "$result" in
    PASS) ((pass_count += 1)) ;;
    WARN) ((warn_count += 1)) ;;
    SKIP) ((skip_count += 1)) ;;
    FAIL) ((fail_count += 1)) ;;
  esac
}

bounded() {
  local seconds=$1
  shift
  timeout --signal=TERM --kill-after=2s "${seconds}s" "$@"
}

only_journal_no_entries_marker() {
  awk 'NF { if ($0 != "-- No entries --" && $0 != "-- No entries found --") unexpected = 1; count += 1 } END { exit !(count > 0 && !unexpected) }' "$1"
}

only_coredump_no_entries_marker() {
  awk 'BEGIN { IGNORECASE = 1 } NF { if ($0 !~ /^No coredumps found\.?$/ && $0 !~ /^No coredump entries found\.?$/) unexpected = 1; count += 1 } END { exit !(count > 0 && !unexpected) }' "$1"
}

if ! command -v timeout >/dev/null 2>&1; then
  printf 'Required command missing: timeout (coreutils)\n' >&2
  exit 69
fi

# Record only a numeric UID; do not infer or publish a username.
init_artifact run-context.txt
uid=$(id -u 2>/dev/null || printf 'unknown')
printf 'uid=%s\n' "$uid" > "$OUTPUT_DIR/run-context.txt"
if [[ $uid == 0 ]]; then
  record run-context PASS run-context.txt "collector ran as root; output directory mode 700"
else
  record run-context WARN run-context.txt "collector ran as uid $uid; root is preferred for complete read-only evidence"
fi

init_artifact system.txt
{
  printf 'collected_utc='
  date -u --iso-8601=seconds 2>/dev/null || printf 'unavailable\n'
  printf 'boot_id=%s\n' "$boot_id"
  printf 'kernel_release='
  uname -r 2>/dev/null || printf 'unavailable\n'
  printf 'architecture='
  uname -m 2>/dev/null || printf 'unavailable\n'
  if [[ -r /etc/os-release ]]; then
    sed -n -E '/^(ID|VERSION_ID|PRETTY_NAME)=/p' /etc/os-release
  fi
} > "$OUTPUT_DIR/system.txt"
if (( boot_id_valid == 1 )) && uname -r >/dev/null 2>&1; then
  record system-identity PASS system.txt "boot ID, collection time, kernel release, architecture, and selected OS release fields"
else
  record system-identity WARN system.txt "some identity fields could not be read"
fi

init_artifact mounts.tsv
if [[ -r /proc/self/mounts ]]; then
  awk 'BEGIN { print "target\tsource\tfstype" } $2 == "/" || $2 == "/boot" || $2 == "/boot/efi" || $2 == "/home" || $2 == "/var" { print $2 "\t" $1 "\t" $3 }' /proc/self/mounts > "$OUTPUT_DIR/mounts.tsv"
  record mounts PASS mounts.tsv "selected mount targets only; no mount options or full mount table"
else
  printf 'selected mount targets unavailable\n' > "$OUTPUT_DIR/mounts.tsv"
  record mounts SKIP mounts.tsv "/proc/self/mounts is not readable"
fi

for package in "${PACKAGES[@]}"; do
  version_artifact="package-$package.txt"
  integrity_artifact="pacman-qkk-$package.txt"
  init_artifact "$version_artifact"
  init_artifact "$integrity_artifact"

  if ! command -v pacman >/dev/null 2>&1; then
    printf 'SKIPPED: pacman is unavailable\n' > "$OUTPUT_DIR/$version_artifact"
    printf 'SKIPPED: pacman is unavailable\n' > "$OUTPUT_DIR/$integrity_artifact"
    record "package-version:$package" SKIP "$version_artifact" "pacman is unavailable"
    record "package-integrity:$package" SKIP "$integrity_artifact" "pacman is unavailable"
    continue
  fi

  bounded "$PACKAGE_TIMEOUT_SECONDS" pacman -Q "$package" > "$OUTPUT_DIR/$version_artifact" 2>&1
  pacman_rc=$?
  if (( pacman_rc == 0 )); then
    record "package-version:$package" PASS "$version_artifact" "installed package version recorded"
    bounded "$PACKAGE_TIMEOUT_SECONDS" pacman -Qkk "$package" > "$OUTPUT_DIR/$integrity_artifact" 2>&1
    integrity_rc=$?
    if (( integrity_rc == 124 || integrity_rc == 137 || integrity_rc == 143 )); then
      record "package-integrity:$package" FAIL "$integrity_artifact" "pacman -Qkk timed out or was terminated (exit $integrity_rc)"
    elif grep -Eiq 'warning:|size mismatch|modified|checksum|sha-?256|digest|hash mismatch|[1-9][0-9]*[[:space:]]+(altered|modified|missing)[[:space:]]+files?' "$OUTPUT_DIR/$integrity_artifact"; then
      record "package-integrity:$package" WARN "$integrity_artifact" "pacman -Qkk reported a file difference; review the private artifact"
    elif (( integrity_rc != 0 )); then
      record "package-integrity:$package" FAIL "$integrity_artifact" "pacman -Qkk exited $integrity_rc"
    else
      record "package-integrity:$package" PASS "$integrity_artifact" "pacman -Qkk reported no differences against local package metadata; this is not independent archive-signature verification"
    fi
  else
    if grep -Eiq 'was not found|not found|is not installed|no package found' "$OUTPUT_DIR/$version_artifact"; then
      record "package-version:$package" WARN "$version_artifact" "selected package is not installed"
      printf 'SKIPPED: package is not installed\n' > "$OUTPUT_DIR/$integrity_artifact"
      record "package-integrity:$package" SKIP "$integrity_artifact" "no installed package to check"
    else
      record "package-version:$package" FAIL "$version_artifact" "pacman -Q exited $pacman_rc"
      printf 'SKIPPED: package version query failed\n' > "$OUTPUT_DIR/$integrity_artifact"
      record "package-integrity:$package" SKIP "$integrity_artifact" "installed state could not be established"
    fi
  fi
done

init_artifact system-service-units.tsv
init_artifact system-failed-units.tsv
if ! command -v systemctl >/dev/null 2>&1; then
  printf 'SKIPPED: systemctl is unavailable\n' > "$OUTPUT_DIR/system-service-units.tsv"
  printf 'SKIPPED: systemctl is unavailable\n' > "$OUTPUT_DIR/system-failed-units.tsv"
  record system-service-units SKIP system-service-units.tsv "systemctl is unavailable"
  record system-failed-units SKIP system-failed-units.tsv "systemctl is unavailable"
else
  unit_raw="$OUTPUT_DIR/.system-units.raw"
  bounded "$TIMEOUT_SECONDS" systemctl --plain list-units --type=service --all --no-legend --no-pager > "$unit_raw" 2>&1
  unit_rc=$?
  if (( unit_rc == 0 )); then
    awk 'NF >= 4 { print $1 "\t" $2 "\t" $3 "\t" $4 }' "$unit_raw" > "$OUTPUT_DIR/system-service-units.tsv"
    record system-service-units PASS system-service-units.tsv "service unit names and states recorded; descriptions omitted"
  else
    printf 'systemctl list-units exited %s\n' "$unit_rc" > "$OUTPUT_DIR/system-service-units.tsv"
    record system-service-units FAIL system-service-units.tsv "systemctl list-units exited $unit_rc"
  fi
  bounded "$TIMEOUT_SECONDS" systemctl --plain --failed --no-legend --no-pager > "$unit_raw" 2>&1
  failed_unit_rc=$?
  if (( failed_unit_rc == 0 )); then
    awk 'NF >= 4 { print $1 "\t" $2 "\t" $3 "\t" $4 }' "$unit_raw" > "$OUTPUT_DIR/system-failed-units.tsv"
    if [[ -s $OUTPUT_DIR/system-failed-units.tsv ]]; then
      record system-failed-units WARN system-failed-units.tsv "one or more failed system units are listed"
    else
      record system-failed-units PASS system-failed-units.tsv "no failed system units are listed"
    fi
  else
    printf 'systemctl --failed exited %s\n' "$failed_unit_rc" > "$OUTPUT_DIR/system-failed-units.tsv"
    record system-failed-units FAIL system-failed-units.tsv "systemctl --failed exited $failed_unit_rc"
  fi
  rm -f -- "$unit_raw"
fi

# Discover user managers from logind; only numeric UIDs are used in output paths.
init_artifact discovered-user-uids.txt
if ! command -v loginctl >/dev/null 2>&1; then
  printf 'SKIPPED: loginctl is unavailable\n' > "$OUTPUT_DIR/discovered-user-uids.txt"
  record user-service-units SKIP discovered-user-uids.txt "loginctl is unavailable; user managers were not guessed"
elif ! command -v systemctl >/dev/null 2>&1; then
  printf 'SKIPPED: systemctl is unavailable\n' > "$OUTPUT_DIR/discovered-user-uids.txt"
  record user-service-units SKIP discovered-user-uids.txt "systemctl is unavailable"
else
  login_raw="$OUTPUT_DIR/.users.raw"
  bounded "$TIMEOUT_SECONDS" loginctl list-users --no-legend > "$login_raw" 2>&1
  login_rc=$?
  if (( login_rc != 0 )); then
    printf 'loginctl list-users exited %s\n' "$login_rc" > "$OUTPUT_DIR/discovered-user-uids.txt"
    record user-service-units FAIL discovered-user-uids.txt "loginctl list-users exited $login_rc"
  else
    awk '$1 ~ /^[0-9]+$/ { print $1 }' "$login_raw" | sort -u > "$OUTPUT_DIR/discovered-user-uids.txt"
    if [[ ! -s $OUTPUT_DIR/discovered-user-uids.txt ]]; then
      record user-service-units SKIP discovered-user-uids.txt "logind reported no user managers; no identity was guessed"
    else
      while IFS= read -r user_uid; do
        [[ $user_uid =~ ^[0-9]+$ ]] || continue
        user_artifact="user-service-units-uid-$user_uid.tsv"
        failed_user_artifact="user-failed-units-uid-$user_uid.tsv"
        init_artifact "$user_artifact"
        init_artifact "$failed_user_artifact"
        if [[ ! -S /run/user/$user_uid/bus ]]; then
          printf 'SKIPPED: no user bus at /run/user/%s/bus\n' "$user_uid" > "$OUTPUT_DIR/$user_artifact"
          printf 'SKIPPED: no user bus at /run/user/%s/bus\n' "$user_uid" > "$OUTPUT_DIR/$failed_user_artifact"
          record "user-service-units:$user_uid" SKIP "$user_artifact" "user manager bus is not available"
          record "user-failed-units:$user_uid" SKIP "$failed_user_artifact" "user manager bus is not available"
          continue
        fi
        bounded "$TIMEOUT_SECONDS" systemctl --user --machine="${user_uid}@.host" --plain list-units --type=service --all --no-legend --no-pager > "$unit_raw" 2>&1
        user_unit_rc=$?
        if (( user_unit_rc == 0 )); then
          awk 'NF >= 4 { print $1 "\t" $2 "\t" $3 "\t" $4 }' "$unit_raw" > "$OUTPUT_DIR/$user_artifact"
          record "user-service-units:$user_uid" PASS "$user_artifact" "discovered user service unit names and states; descriptions omitted"
        else
          printf 'systemctl --user list-units exited %s\n' "$user_unit_rc" > "$OUTPUT_DIR/$user_artifact"
          record "user-service-units:$user_uid" FAIL "$user_artifact" "systemctl --user list-units exited $user_unit_rc"
        fi
        bounded "$TIMEOUT_SECONDS" systemctl --user --machine="${user_uid}@.host" --plain --failed --no-legend --no-pager > "$unit_raw" 2>&1
        user_failed_rc=$?
        if (( user_failed_rc == 0 )); then
          awk 'NF >= 4 { print $1 "\t" $2 "\t" $3 "\t" $4 }' "$unit_raw" > "$OUTPUT_DIR/$failed_user_artifact"
          if [[ -s $OUTPUT_DIR/$failed_user_artifact ]]; then
            record "user-failed-units:$user_uid" WARN "$failed_user_artifact" "one or more failed user units are listed"
          else
            record "user-failed-units:$user_uid" PASS "$failed_user_artifact" "no failed user units are listed"
          fi
        else
          printf 'systemctl --user --failed exited %s\n' "$user_failed_rc" > "$OUTPUT_DIR/$failed_user_artifact"
          record "user-failed-units:$user_uid" FAIL "$failed_user_artifact" "systemctl --user --failed exited $user_failed_rc"
        fi
      done < "$OUTPUT_DIR/discovered-user-uids.txt"
    fi
  fi
  rm -f -- "$login_raw"
  rm -f -- "$unit_raw"
fi

init_artifact current-boot-kernel-events.txt
if ! command -v journalctl >/dev/null 2>&1; then
  printf 'SKIPPED: journalctl is unavailable\n' > "$OUTPUT_DIR/current-boot-kernel-events.txt"
  record current-boot-kernel-events SKIP current-boot-kernel-events.txt "journalctl is unavailable"
else
  journal_raw="$OUTPUT_DIR/.journal.raw"
  bounded "$TIMEOUT_SECONDS" journalctl --boot=0 --dmesg --no-pager --priority=warning --output=short-monotonic --grep="$JOURNAL_PATTERN" --lines=200 > "$journal_raw" 2>&1
  journal_rc=$?
  if (( journal_rc == 0 || journal_rc == 1 )) && only_journal_no_entries_marker "$journal_raw"; then
    : > "$OUTPUT_DIR/current-boot-kernel-events.txt"
    record current-boot-kernel-events PASS current-boot-kernel-events.txt "no filtered crash or storage warning/error entries found in the current boot"
  elif (( journal_rc != 0 )); then
    printf 'journalctl filtered current-boot kernel query exited %s\n' "$journal_rc" > "$OUTPUT_DIR/current-boot-kernel-events.txt"
    record current-boot-kernel-events FAIL current-boot-kernel-events.txt "journalctl filtered query exited $journal_rc"
  else
    sed -E \
      -e 's/(authorization|proxy-authorization)([=:][[:space:]]*)(Bearer[[:space:]]+|Basic[[:space:]]+)?[^[:space:]]+/\1\2[REDACTED]/Ig' \
      -e 's/(Bearer|Basic)[[:space:]]+[A-Za-z0-9._~+\/=:-]+/\1 [REDACTED]/Ig' \
      -e 's/(password|passwd|passphrase|psk|ssid|token|secret|api[_-]?key|bearer)([=:][[:space:]]*)[^[:space:]]+/\1\2[REDACTED]/Ig' \
      "$journal_raw" | awk 'length($0) > 1000 { print substr($0, 1, 1000) " [TRUNCATED]"; next } { print }' > "$OUTPUT_DIR/current-boot-kernel-events.txt"
    if [[ -s $OUTPUT_DIR/current-boot-kernel-events.txt ]]; then
      event_count=$(wc -l < "$OUTPUT_DIR/current-boot-kernel-events.txt" | tr -d ' ')
      record current-boot-kernel-events WARN current-boot-kernel-events.txt "$event_count filtered matching kernel warning/error entries; review timestamps and context"
    else
      record current-boot-kernel-events PASS current-boot-kernel-events.txt "no filtered crash or storage warning/error entries found in the current boot"
    fi
  fi
  rm -f -- "$journal_raw"
fi

init_artifact current-boot-coredumps.txt
if (( boot_id_valid == 0 )); then
  printf 'SKIPPED: current boot ID is unavailable or invalid\n' > "$OUTPUT_DIR/current-boot-coredumps.txt"
  record current-boot-coredumps SKIP current-boot-coredumps.txt "current boot ID failed strict validation; no unfiltered coredump list was requested"
elif ! command -v coredumpctl >/dev/null 2>&1; then
  printf 'SKIPPED: coredumpctl is unavailable\n' > "$OUTPUT_DIR/current-boot-coredumps.txt"
  record current-boot-coredumps SKIP current-boot-coredumps.txt "coredumpctl is unavailable; no core was opened or dumped"
else
  coredump_raw="$OUTPUT_DIR/.coredump.raw"
  bounded "$TIMEOUT_SECONDS" coredumpctl --no-pager --no-legend -n 200 list "_BOOT_ID=$boot_id_match" > "$coredump_raw" 2>&1
  coredump_rc=$?
  if (( coredump_rc == 0 || coredump_rc == 1 )) && only_coredump_no_entries_marker "$coredump_raw"; then
    : > "$OUTPUT_DIR/current-boot-coredumps.txt"
    record current-boot-coredumps PASS current-boot-coredumps.txt "no current-boot coredumps listed; no core was opened or dumped"
  elif (( coredump_rc != 0 )); then
    printf 'coredumpctl current-boot metadata query exited %s\n' "$coredump_rc" > "$OUTPUT_DIR/current-boot-coredumps.txt"
    record current-boot-coredumps FAIL current-boot-coredumps.txt "coredumpctl list exited $coredump_rc"
  elif [[ ! -s $coredump_raw ]]; then
    : > "$OUTPUT_DIR/current-boot-coredumps.txt"
    record current-boot-coredumps PASS current-boot-coredumps.txt "no current-boot coredumps listed; no core was opened or dumped"
  else
    cat "$coredump_raw" > "$OUTPUT_DIR/current-boot-coredumps.txt"
    record current-boot-coredumps WARN current-boot-coredumps.txt "current-boot coredump metadata is listed; no core was opened or dumped"
  fi
  rm -f -- "$coredump_raw"
fi

init_artifact raspberry-pi-thermal.txt
if ! command -v vcgencmd >/dev/null 2>&1; then
  printf 'SKIPPED: vcgencmd is unavailable\n' > "$OUTPUT_DIR/raspberry-pi-thermal.txt"
  record raspberry-pi-thermal SKIP raspberry-pi-thermal.txt "vcgencmd is unavailable"
else
  bounded "$TIMEOUT_SECONDS" vcgencmd measure_temp > "$OUTPUT_DIR/raspberry-pi-thermal.txt" 2>&1
  temp_rc=$?
  if (( temp_rc == 0 )); then
    record raspberry-pi-temperature PASS raspberry-pi-thermal.txt "firmware temperature reading recorded; no threshold inferred"
  else
    record raspberry-pi-temperature FAIL raspberry-pi-thermal.txt "vcgencmd measure_temp exited $temp_rc"
  fi
  bounded "$TIMEOUT_SECONDS" vcgencmd get_throttled >> "$OUTPUT_DIR/raspberry-pi-thermal.txt" 2>&1
  throttle_rc=$?
  if (( throttle_rc != 0 )); then
    record raspberry-pi-throttling FAIL raspberry-pi-thermal.txt "vcgencmd get_throttled exited $throttle_rc"
  elif grep -Eiq 'throttled=0x0+$' "$OUTPUT_DIR/raspberry-pi-thermal.txt"; then
    record raspberry-pi-throttling PASS raspberry-pi-thermal.txt "firmware reports no current or historical throttling flags"
  elif grep -Eiq 'throttled=0x[0-9a-f]+' "$OUTPUT_DIR/raspberry-pi-thermal.txt"; then
    record raspberry-pi-throttling WARN raspberry-pi-thermal.txt "firmware reports nonzero current or historical throttling flags"
  else
    record raspberry-pi-throttling WARN raspberry-pi-thermal.txt "throttling response was not recognized; review artifact"
  fi
fi

nvme_sysfs_found=0
for controller_path in /sys/class/nvme/nvme[0-9]*; do
  [[ -e $controller_path ]] || continue
  controller=${controller_path##*/}
  [[ $controller =~ ^nvme[0-9]+$ ]] || continue
  nvme_sysfs_found=1
  smart_artifact="smart-${controller}.txt"
  init_artifact "$smart_artifact"
  device="/dev/$controller"
  if ! command -v smartctl >/dev/null 2>&1; then
    printf 'SKIPPED: smartctl is unavailable\n' > "$OUTPUT_DIR/$smart_artifact"
    record "nvme-smart:$controller" SKIP "$smart_artifact" "smartctl is unavailable; no device was opened"
    continue
  fi
  if [[ ! -e $device ]]; then
    printf 'SKIPPED: controller device node is absent\n' > "$OUTPUT_DIR/$smart_artifact"
    record "nvme-smart:$controller" SKIP "$smart_artifact" "device node $device is absent"
    continue
  fi
  smart_raw="$OUTPUT_DIR/.$smart_artifact.raw"
  bounded "$TIMEOUT_SECONDS" smartctl -H -A "$device" > "$smart_raw" 2>&1
  smart_rc=$?
  awk 'BEGIN { IGNORECASE=1 } /critical warning|temperature:|available spare|percentage used|data units read|data units written|host read commands|host write commands|controller busy time|power cycles|power on hours|unsafe shutdowns|media and data integrity errors|error information log entries|warning comp\. temperature time|critical comp\. temperature time|overall-health|smart health status/ { print }' "$smart_raw" > "$OUTPUT_DIR/$smart_artifact"
  rm -f -- "$smart_raw"
  if grep -Eiq 'critical warning: *0x0*[1-9a-f][0-9a-f]*|media and data integrity errors: *[1-9][0-9]*|error information log entries: *[1-9][0-9]*' "$OUTPUT_DIR/$smart_artifact"; then
    record "nvme-smart:$controller" WARN "$smart_artifact" "SMART reports a nonzero critical-warning or error counter; model and serial omitted"
  elif (( smart_rc != 0 )); then
    printf 'smartctl -H -A exited %s\n' "$smart_rc" >> "$OUTPUT_DIR/$smart_artifact"
    record "nvme-smart:$controller" FAIL "$smart_artifact" "smartctl exited $smart_rc; device identity fields were omitted"
  elif [[ ! -s $OUTPUT_DIR/$smart_artifact ]]; then
    printf 'No selected SMART attributes were returned.\n' > "$OUTPUT_DIR/$smart_artifact"
    record "nvme-smart:$controller" WARN "$smart_artifact" "smartctl returned no selected attributes; device identity fields were omitted"
  elif grep -Eiq 'critical warning: *0x0+$|SMART overall-health self-assessment test result: PASSED|SMART Health Status: OK' "$OUTPUT_DIR/$smart_artifact"; then
    record "nvme-smart:$controller" PASS "$smart_artifact" "selected SMART health fields recorded; model and serial omitted"
  else
    record "nvme-smart:$controller" WARN "$smart_artifact" "review selected SMART health fields; model and serial omitted"
  fi
done
if (( nvme_sysfs_found == 0 )); then
  init_artifact nvme-smart.txt
  printf 'SKIPPED: no NVMe controller was discovered under /sys/class/nvme\n' > "$OUTPUT_DIR/nvme-smart.txt"
  record nvme-smart SKIP nvme-smart.txt "no NVMe controller discovered; no device path was guessed"
fi

init_artifact summary.txt
{
  printf 'Read-only Pi stability collection\n'
  printf 'Output directory: %s\n' "$OUTPUT_DIR"
  printf 'Status counts: PASS=%s WARN=%s SKIP=%s FAIL=%s\n' "$pass_count" "$warn_count" "$skip_count" "$fail_count"
  printf 'Review status.tsv and the referenced private artifacts. WARN means evidence needs review; SKIP means unavailable; FAIL means a query could not be completed.\n'
} > "$OUTPUT_DIR/summary.txt"

if (( fail_count > 0 )); then
  exit 1
fi
exit 0
