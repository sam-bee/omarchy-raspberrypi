#!/bin/bash

set -euo pipefail

source "$(dirname "$0")/base-test.sh"

test_tmp=$(mktemp -d)
trap 'rm -rf "$test_tmp"' EXIT

stub_bin="$test_tmp/bin"
git_log="$test_tmp/git.log"
mkdir -p "$stub_bin"

cat >"$stub_bin/uname" <<'SH'
#!/bin/bash
if [[ -n ${TEST_UNAME:-} ]]; then
  printf '%s\n' "$TEST_UNAME"
else
  /usr/bin/uname "$@"
fi
SH
chmod +x "$stub_bin/uname"

cat >"$stub_bin/checkupdates" <<'SH'
#!/bin/bash
case "${TEST_CHECKUPDATES:-updates}" in
  updates)
    printf 'linux 6.1-1 -> 6.1-2\nomarchy 4.0.0-1 -> 4.0.1-1\nomarchy-settings 4.0.0-1 -> 4.0.1-1\nomarchy-dev 4.1.0-1 -> 4.1.1-1\nomarchy-settings-dev 4.1.0-1 -> 4.1.1-1\n'
    exit 0
    ;;
  none)
    exit 2
    ;;
  fail)
    echo "check failed" >&2
    exit 1
    ;;
esac
SH
chmod +x "$stub_bin/checkupdates"

cat >"$stub_bin/pacman" <<'SH'
#!/bin/bash
case "$1" in
  -Qo)
    [[ ${TEST_PACKAGE_PROVENANCE:-} == stable ]] || exit 1
    case "${4:-$3}" in
      */version) printf 'omarchy\n' ;;
      */config) printf 'omarchy-settings\n' ;;
      *) exit 1 ;;
    esac
    exit 0
    ;;
  -Qq)
    case "${TEST_INSTALLED_PACKAGE:-omarchy}" in
      omarchy)
        [[ $2 == "omarchy" ]]; exit $?
        ;;
      omarchy-dev)
        [[ $2 == "omarchy-dev" ]]; exit $?
        ;;
      both)
        [[ $2 == "omarchy" || $2 == "omarchy-dev" ]]; exit $?
        ;;
      none)
        exit 1
        ;;
    esac
    ;;
esac
exit 0
SH
chmod +x "$stub_bin/pacman"

cat >"$stub_bin/git" <<'SH'
#!/bin/bash

printf '%s\n' "$*" >>"$TEST_GIT_LOG"

[[ $1 == "-C" ]] || exit 1
shift 2

case "$1" in
  fetch)
    [[ ${TEST_GIT_FETCH:-ok} == "ok" ]]
    ;;
  rev-parse)
    case "$2" in
      --is-inside-work-tree)
        [[ ${TEST_GIT_CHECKOUT:-yes} == "yes" ]] || exit 1
        echo true
        ;;
      --abbrev-ref)
        [[ ${TEST_GIT_UPSTREAM:-origin/quattro} != "none" ]] || exit 1
        echo "${TEST_GIT_UPSTREAM:-origin/quattro}"
        ;;
      *)
        exit 1
        ;;
    esac
    ;;
  rev-list)
    echo "${TEST_GIT_BEHIND:-0}"
    ;;
  *)
    exit 1
    ;;
esac
SH
chmod +x "$stub_bin/git"

run_checker() {
  OMARCHY_PATH="${TEST_OMARCHY_PATH:-/usr/share/omarchy}" \
    TEST_GIT_LOG="$git_log" \
    PATH="$stub_bin:$PATH" \
    "$ROOT/bin/omarchy-update-available"
}

capture_checker() {
  local stdout_file="$1"
  local stderr_file="$2"
  shift 2

  set +e
  (
    export "$@"
    run_checker
  ) >"$stdout_file" 2>"$stderr_file"
  local status=$?
  set -e
  return "$status"
}

stdout="$test_tmp/stdout"
stderr="$test_tmp/stderr"

if capture_checker "$stdout" "$stderr" TEST_CHECKUPDATES=updates TEST_INSTALLED_PACKAGE=omarchy; then
  status=0
else
  status=$?
fi
[[ $status -eq 0 ]] || fail "update checker exits successfully when omarchy update is available"
grep -q '^omarchy ' "$stdout" || fail "update checker prints omarchy updates"
! grep -q '^omarchy-settings ' "$stdout" || fail "update checker ignores omarchy-settings updates"
! grep -q '^linux ' "$stdout" || fail "update checker ignores non-Omarchy package updates"
! grep -q '^omarchy-dev ' "$stdout" || fail "update checker ignores omarchy-dev when omarchy is installed"
pass "update checker detects installed omarchy package updates"

if capture_checker "$stdout" "$stderr" TEST_CHECKUPDATES=updates TEST_INSTALLED_PACKAGE=omarchy-dev; then
  status=0
else
  status=$?
fi
[[ $status -eq 0 ]] || fail "update checker exits successfully when omarchy-dev update is available"
grep -q '^omarchy-dev ' "$stdout" || fail "update checker prints omarchy-dev updates"
! grep -q '^omarchy-settings-dev ' "$stdout" || fail "update checker ignores omarchy-settings-dev updates"
! grep -q '^omarchy ' "$stdout" || fail "update checker ignores omarchy when omarchy-dev is installed"
pass "update checker detects installed omarchy-dev package updates"

if capture_checker "$stdout" "$stderr" TEST_CHECKUPDATES=updates TEST_INSTALLED_PACKAGE=both; then
  status=0
else
  status=$?
fi
[[ $status -eq 0 ]] || fail "update checker prefers omarchy-dev when both packages are installed"
grep -q '^omarchy-dev ' "$stdout" || fail "update checker prints omarchy-dev when both packages are installed"
! grep -q '^omarchy ' "$stdout" || fail "update checker ignores omarchy when omarchy-dev is installed"
pass "update checker prefers omarchy-dev over omarchy"

if capture_checker "$stdout" "$stderr" TEST_CHECKUPDATES=updates TEST_INSTALLED_PACKAGE=none; then
  status=0
else
  status=$?
fi
[[ $status -eq 1 ]] || fail "update checker exits non-zero when no Omarchy package is installed"
[[ ! -s $stderr ]] || fail "update checker is quiet when no Omarchy package is installed"
pass "update checker ignores systems without omarchy or omarchy-dev installed"

if capture_checker "$stdout" "$stderr" TEST_CHECKUPDATES=none TEST_INSTALLED_PACKAGE=omarchy; then
  status=0
else
  status=$?
fi
[[ $status -eq 1 ]] || fail "update checker exits non-zero when no updates are available"
grep -q '^Omarchy is up to date$' "$stdout" || fail "update checker prints up-to-date message"
pass "update checker reports up-to-date Omarchy packages"

: >"$git_log"
if capture_checker "$stdout" "$stderr" \
  TEST_CHECKUPDATES=none \
  TEST_INSTALLED_PACKAGE=none \
  TEST_OMARCHY_PATH="$test_tmp/checkout" \
  TEST_GIT_BEHIND=2; then
  status=0
else
  status=$?
fi
[[ $status -eq 0 ]] || fail "update checker exits successfully when dev commits are available"
grep -Fx 'omarchy-dev-checkout 2 new commits on origin/quattro' "$stdout" >/dev/null ||
  fail "update checker reports available dev commits" "$(cat "$stdout")"
grep -Fx -- "-C $test_tmp/checkout fetch --quiet" "$git_log" >/dev/null ||
  fail "update checker fetches the dev checkout upstream" "$(cat "$git_log")"
pass "update checker detects new commits in the dev checkout"

if capture_checker "$stdout" "$stderr" \
  TEST_CHECKUPDATES=none \
  TEST_INSTALLED_PACKAGE=none \
  TEST_OMARCHY_PATH="$test_tmp/checkout" \
  TEST_GIT_BEHIND=0; then
  status=0
else
  status=$?
fi
[[ $status -eq 1 ]] || fail "update checker exits non-zero when the dev checkout is current"
grep -q '^Omarchy is up to date$' "$stdout" || fail "update checker reports a current dev checkout"
pass "update checker ignores a current dev checkout"

if capture_checker "$stdout" "$stderr" \
  TEST_CHECKUPDATES=none \
  TEST_INSTALLED_PACKAGE=none \
  TEST_OMARCHY_PATH="$test_tmp/checkout" \
  TEST_GIT_BEHIND=1 \
  TEST_GIT_FETCH=fail; then
  status=0
else
  status=$?
fi
[[ $status -eq 0 ]] || fail "update checker uses cached upstream state when fetch fails"
grep -Fx 'omarchy-dev-checkout 1 new commit on origin/quattro' "$stdout" >/dev/null ||
  fail "update checker reports cached dev commits after a fetch failure" "$(cat "$stdout")"
[[ ! -s $stderr ]] || fail "update checker keeps dev fetch failures quiet" "$(cat "$stderr")"
pass "update checker uses cached dev state when fetching is unavailable"

pi_checkout="$test_tmp/pi-checkout"
mkdir -p "$pi_checkout/install/arm64"
touch "$pi_checkout/.omarchy-pi-source-commit"
cat >"$pi_checkout/install/arm64/update-source.py" <<'SH'
#!/bin/bash
printf '%s\n' "${TEST_PI_SOURCE_OUTPUT:-up-to-date 0000000000000000000000000000000000000000}"
exit "${TEST_PI_SOURCE_STATUS:-0}"
SH
chmod +x "$pi_checkout/install/arm64/update-source.py"

if capture_checker "$stdout" "$stderr" \
  TEST_CHECKUPDATES=none \
  TEST_INSTALLED_PACKAGE=none \
  TEST_OMARCHY_PATH="$pi_checkout" \
  TEST_GIT_BEHIND=0 \
  TEST_PI_SOURCE_OUTPUT="update-available 1111111111111111111111111111111111111111 (current 0000000000000000000000000000000000000000)"; then
  status=0
else
  status=$?
fi
[[ $status -eq 0 ]] || fail "update checker reports a Pi source update"
grep -Fx 'omarchy-pi-source 1111111111111111111111111111111111111111 available' "$stdout" >/dev/null ||
  fail "update checker prints the Pi source revision" "$(cat "$stdout")"
pass "update checker detects an available Pi source release"

if capture_checker "$stdout" "$stderr" \
  TEST_CHECKUPDATES=none \
  TEST_INSTALLED_PACKAGE=none \
  TEST_OMARCHY_PATH="$pi_checkout" \
  TEST_GIT_BEHIND=0 \
  TEST_PI_SOURCE_OUTPUT="up-to-date 0000000000000000000000000000000000000000"; then
  status=0
else
  status=$?
fi
[[ $status -eq 1 ]] || fail "update checker reports a current Pi source release as no update"
grep -Fx 'Omarchy is up to date' "$stdout" >/dev/null || fail "update checker reports current Pi source state"
pass "update checker accepts a successful Pi source no-change check"

if capture_checker "$stdout" "$stderr" \
  TEST_CHECKUPDATES=updates \
  TEST_INSTALLED_PACKAGE=none \
  TEST_OMARCHY_PATH="$pi_checkout" \
  TEST_GIT_BEHIND=0 \
  TEST_PI_SOURCE_OUTPUT="up-to-date 0000000000000000000000000000000000000000"; then
  status=0
else
  status=$?
fi
[[ $status -eq 0 ]] || fail "update checker reports ALARM package updates"
grep -Fx 'linux 6.1-1 -> 6.1-2' "$stdout" >/dev/null ||
  fail "update checker prints ALARM package updates" "$(cat "$stdout")"
pass "update checker checks ALARM packages on Pi source installs"

if capture_checker "$stdout" "$stderr" \
  TEST_CHECKUPDATES=none \
  TEST_INSTALLED_PACKAGE=none \
  TEST_OMARCHY_PATH="$pi_checkout" \
  TEST_GIT_BEHIND=0 \
  TEST_PI_SOURCE_STATUS=2 \
  TEST_PI_SOURCE_OUTPUT="source update unavailable"; then
  status=0
else
  status=$?
fi
[[ $status -eq 0 ]] || fail "update checker does not hide a failed Pi source check as up-to-date"
grep -Fx 'omarchy-pi-source update check failed' "$stdout" >/dev/null ||
  fail "update checker surfaces a failed Pi source check" "$(cat "$stdout")"
! grep -Fx 'Omarchy is up to date' "$stdout" >/dev/null || fail "failed Pi source check is not reported as up-to-date"
pass "update checker surfaces Pi source resolution failures"

if capture_checker "$stdout" "$stderr" \
  TEST_CHECKUPDATES=fail \
  TEST_INSTALLED_PACKAGE=none \
  TEST_OMARCHY_PATH="$pi_checkout" \
  TEST_GIT_BEHIND=0 \
  TEST_PI_SOURCE_OUTPUT="up-to-date 0000000000000000000000000000000000000000"; then
  status=0
else
  status=$?
fi
[[ $status -eq 0 ]] || fail "update checker surfaces an ALARM package check failure"
grep -Fx 'ALARM package update check failed' "$stdout" >/dev/null ||
  fail "update checker reports an ALARM package check failure" "$(cat "$stdout")"
! grep -Fx 'Omarchy is up to date' "$stdout" >/dev/null || fail "failed ALARM package check is not reported as up-to-date"
pass "update checker surfaces ALARM package resolution failures"

packaged_runtime="$test_tmp/packaged-runtime"
mkdir -p "$packaged_runtime/config" "$packaged_runtime/install/arm64"
touch "$packaged_runtime/version"
printf '%s\n' '{"schema_version":1,"runtime_mode":"packaged","source_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}' >"$packaged_runtime/.omarchy-pi-packaged.json"
cp "$ROOT/install/arm64/update_packages.py" "$packaged_runtime/install/arm64/update_packages.py"
candidate_root="$test_tmp/package-candidate"
python3 - "$candidate_root" <<'PY'
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

root = Path(sys.argv[1])
(root / "source/migrations").mkdir(parents=True)
(root / "source/install/arm64").mkdir(parents=True)
(root / "source/install/arm64/migrations.allowlist").write_text("# reviewed\n")
(root / "source/.omarchy-pi-source-commit").write_text("b" * 40 + "\n")

source_archive = root / "source.tar"
with tarfile.open(source_archive, "w") as stream:
    for path in sorted((root / "source").rglob("*")):
        if path.name == ".omarchy-pi-source-commit":
            continue
        stream.add(path, arcname=path.relative_to(root / "source").as_posix(), recursive=False)
source_sha256 = hashlib.sha256(source_archive.read_bytes()).hexdigest()

def archive(directory, name, package_name, version, revision, package_sha256):
    path = root / directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    settings = "omarchy-settings"
    metadata = f"pkgname = {package_name}\npkgver = {version}\narch = aarch64\n"
    if package_name == "omarchy":
        metadata += f"depend = {settings}={version}\n"
    marker = json.dumps({
        "schema_version": 1,
        "layout": "packaged",
        "runtime_mode": "packaged",
        "channel": "stable",
        "version": version,
        "source_revision": revision,
        "source_sha256": package_sha256,
    }, sort_keys=True).encode() + b"\n"
    with tarfile.open(path, "w") as stream:
        def add(member, body):
            info = tarfile.TarInfo(member)
            info.size = len(body)
            info.mode = 0o644
            stream.addfile(info, io.BytesIO(body))
        add(".PKGINFO", metadata.encode())
        add("usr/share/omarchy/.omarchy-pi-source-commit", (revision + "\n").encode())
        add("usr/share/omarchy/.omarchy-pi-packaged.json", marker)
    return {
        "name": package_name,
        "version": version,
        "architecture": "aarch64",
        "filename": f"{directory}/{name}",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "signature": "optional",
    }

packages = [archive("packages", f"{name}-1.0-1-aarch64.pkg.tar.zst", name, "1.0-1", "b" * 40, source_sha256) for name in ("omarchy", "omarchy-settings")]
previous = [archive("previous", f"{name}-0.9-1-aarch64.pkg.tar.zst", name, "0.9-1", "a" * 40, "c" * 64) for name in ("omarchy", "omarchy-settings")]
(root / "candidate.json").write_text(json.dumps({
    "schema_version": 1,
    "architecture": "aarch64",
    "channel": "stable",
    "source_revision": "b" * 40,
    "source": {"tree": "source", "archive": "source.tar", "archive_sha256": source_sha256},
    "packages": packages,
    "previous_packages": previous,
}))
PY

if capture_checker "$stdout" "$stderr" \
  TEST_UNAME=aarch64 \
  TEST_PACKAGE_PROVENANCE=stable \
  TEST_CHECKUPDATES=fail \
  TEST_INSTALLED_PACKAGE=none \
  TEST_OMARCHY_PATH="$packaged_runtime" \
  OMARCHY_PI_TESTING=1 \
  OMARCHY_PI_TEST_RUNTIME_ROOT="$packaged_runtime" \
  OMARCHY_PI_TEST_CANDIDATE_ROOT="$candidate_root"; then
  status=0
else
  status=$?
fi
[[ $status -eq 0 ]] || fail "update checker detects a reviewed packaged ARM candidate" "$(cat "$stdout" "$stderr")"
grep -Fx "omarchy-stable-candidate $(printf 'b%.0s' {1..40}) available" "$stdout" >/dev/null ||
  fail "update checker reports packaged candidate provenance" "$(cat "$stdout")"
pass "update checker detects a reviewed packaged ARM candidate"
