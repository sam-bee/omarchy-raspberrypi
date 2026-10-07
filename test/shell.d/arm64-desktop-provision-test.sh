#!/bin/bash

set -euo pipefail
source "$(dirname "$0")/base-test.sh"

require_command git
require_command realpath
require_command tar
require_command stat
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
grep -Fq '/etc/pam.d/omarchy-lock-password' <<<"$dry_run_output" || fail "generic dry-run stages lock password PAM policy"
[[ ! -e $target/usr/share/omarchy-pi ]] || fail "generic dry-run does not populate the target"
[[ ! -e $target/etc/skel ]] || fail "generic dry-run does not create skeleton files"
pass "generic root provisioning dry-run is non-mutating"

packaged_target="$test_tmp/packaged-root"
mkdir -p "$packaged_target/etc" "$packaged_target/usr/bin" "$packaged_target/usr/share/omarchy" "$packaged_target/usr/libexec/omarchy-pi" "$packaged_target/usr/lib/systemd/system" "$packaged_target/usr/lib/systemd/user" "$packaged_target/var"
printf 'root:x:0:0:root:/root:/bin/bash\n' >"$packaged_target/etc/passwd"
printf 'root:x:0:\n' >"$packaged_target/etc/group"
git -C "$test_checkout" rev-parse HEAD >"$packaged_target/usr/share/omarchy/.omarchy-pi-source-commit"
for relative in \
  usr/libexec/omarchy-pi/start-uwsm-session.sh \
  usr/libexec/omarchy-pi/verify-hypr-rdp-runtime.py \
  usr/libexec/omarchy-pi/ensure-headless-output.sh \
  usr/lib/systemd/system/omarchy-pi-uwsm-session@.service \
  usr/lib/systemd/user/omarchy-pi-hypr-rdp.service; do
  printf 'packaged fixture\n' >"$packaged_target/$relative"
done
packaged_dry_run_output=$(bash "$ROOT/install/arm64/provision-desktop-root.sh" \
  --rootfs "$packaged_target" --source-checkout "$test_checkout" \
  --runtime-layout packaged --dry-run)
grep -Fq '/usr/share/omarchy/install/arm64/setup-user-agents.sh' <<<"$packaged_dry_run_output" ||
  fail "packaged dry-run stages the Pi helper extension under the package runtime"
grep -Fq 'runtime mode: packaged' <<<"$packaged_dry_run_output" ||
  fail "packaged dry-run identifies the selected runtime mode"
if grep -Fq 'extract clean Omarchy source' <<<"$packaged_dry_run_output" ||
  grep -Fq 'copy Omarchy release' <<<"$packaged_dry_run_output"; then
  fail "packaged dry-run does not stage a source release"
fi
[[ ! -e $packaged_target/usr/share/omarchy/install ]] ||
  fail "packaged dry-run does not mutate the package runtime"
pass "packaged provisioning dry-run keeps the package-owned runtime and omits source releases"

assert_mode() {
  local expected=$1 path=$2 actual
  actual=$(stat -c '%a' -- "$path")
  [[ $actual == "$expected" ]] || fail "mode $expected on $path" "actual mode: $actual"
}

# Apply the generic payload under a restrictive build umask in a disposable
# user namespace. This checks the generated environment, source readability,
# and skeleton directory traversal without touching the host filesystem.
if command -v unshare >/dev/null && unshare --user --map-root-user true 2>/dev/null; then
  apply_target="$test_tmp/apply-root"
  mkdir -p "$apply_target/etc" "$apply_target/usr" "$apply_target/var"
  printf 'root:x:0:0:root:/root:/bin/bash\n' >"$apply_target/etc/passwd"
  printf 'root:x:0:\n' >"$apply_target/etc/group"
  mkdir -p "$apply_target/usr/bin" "$apply_target/usr/share/omarchy-pi" "$apply_target/usr/lib/systemd/system"
  printf 'hypr-rdp fixture\n' >"$apply_target/usr/bin/hypr-rdp"
  sha256sum "$apply_target/usr/bin/hypr-rdp" | cut -d' ' -f1 >"$apply_target/usr/share/omarchy-pi/hypr-rdp.sha256"
  cat >"$apply_target/usr/lib/systemd/system/NetworkManager.service" <<'EOF'
[Unit]
Description=NetworkManager fixture

[Install]
WantedBy=multi-user.target
Also=NetworkManager-dispatcher.service NetworkManager-wait-online.service
EOF
  for unit in NetworkManager-dispatcher.service NetworkManager-wait-online.service; do
    printf '[Unit]\nDescription=%s fixture\n' "$unit" >"$apply_target/usr/lib/systemd/system/$unit"
  done
  cat >"$apply_target/usr/lib/systemd/system/bluetooth.service" <<'EOF'
[Unit]
Description=Bluetooth fixture

[Install]
WantedBy=bluetooth.target
Alias=dbus-org.bluez.service
EOF
  networkd_links=(
    etc/systemd/system/multi-user.target.wants/systemd-networkd.service
    etc/systemd/system/sockets.target.wants/systemd-networkd.socket
    etc/systemd/system/network-online.target.wants/systemd-networkd-wait-online.service
    etc/systemd/system/network.target.wants/systemd-networkd.service
    etc/systemd/system/dbus-org.freedesktop.network1.service
    etc/systemd/system/sockets.target.wants/systemd-networkd-resolve-hook.socket
    etc/systemd/system/sockets.target.wants/systemd-networkd-varlink-metrics.socket
    etc/systemd/system/sockets.target.wants/systemd-networkd-varlink.socket
  )
  for relative in "${networkd_links[@]}"; do
    mkdir -p "$(dirname "$apply_target/$relative")"
    ln -s /usr/lib/systemd/system/systemd-networkd.service "$apply_target/$relative"
  done
  mkdir -p "$apply_target/etc/systemd/system/multi-user.target.wants"
  ln -s /usr/lib/systemd/system/systemd-resolved.service \
    "$apply_target/etc/systemd/system/multi-user.target.wants/systemd-resolved.service"
  apply_output=$(unshare --user --map-root-user env PATH="$PATH" bash -c '
    umask 077
    bash "$1" --rootfs "$2" --source-checkout "$3"
  ' _ "$ROOT/install/arm64/provision-desktop-root.sh" "$apply_target" "$test_checkout" 2>&1) ||
    fail "generic provisioning applies under umask 077" "$apply_output"
  env_file="$apply_target/etc/skel/.config/uwsm/env.d/90-omarchy-pi"
  grep -Fq '$HOME/.local/share/mise/shims' "$env_file" || fail "generic UWSM environment includes mise shims"
  grep -Fq '$HOME/.local/bin' "$env_file" || fail "generic UWSM environment includes the user bin directory"
  assert_mode 755 "$apply_target/usr/share/omarchy-pi"
  assert_mode 755 "$apply_target/usr/share/omarchy-pi/bin"
  assert_mode 755 "$apply_target/usr/share/omarchy-pi/bin/omarchy-theme-set"
  assert_mode 755 "$apply_target/etc/skel/.config"
  assert_mode 755 "$apply_target/etc/skel/.config/uwsm/env.d"
  [[ -f "$apply_target/usr/share/omarchy-pi/.source-revision" ]] || fail "source revision is added beside the package digest"
  [[ $(<"$apply_target/usr/share/omarchy-pi/hypr-rdp.sha256") == "$(sha256sum "$apply_target/usr/bin/hypr-rdp" | cut -d' ' -f1)" ]] ||
    fail "package RDP digest is preserved and remains valid"
  for relative in "${networkd_links[@]}"; do
    [[ ! -e "$apply_target/$relative" && ! -L "$apply_target/$relative" ]] ||
      fail "networkd enablement is removed: $relative"
  done
  [[ -L "$apply_target/etc/systemd/system/multi-user.target.wants/systemd-resolved.service" ]] ||
    fail "systemd-resolved enablement is retained"
  [[ -L "$apply_target/etc/systemd/system/multi-user.target.wants/NetworkManager.service" ]] ||
    fail "NetworkManager is enabled in the target root"
  [[ -L "$apply_target/etc/systemd/system/bluetooth.target.wants/bluetooth.service" ]] ||
    fail "Bluetooth is enabled in the target root"
  [[ $(<"$apply_target/etc/systemd/system-preset/00-omarchy-pi-networkd.preset") == 'disable systemd-networkd*' ]] ||
    fail "target root carries the networkd-disable preset"
  pass "generic provisioning keeps public source and skeleton paths traversable under umask 077"

  fake_bin="$test_tmp/fake-bin"
  mkdir -p "$fake_bin"
  cat >"$fake_bin/useradd" <<'EOF'
#!/bin/bash
set -euo pipefail
root=$2
home=$5
login=${!#}
mkdir -p -- "$root$home"
cp -a -- "$root/etc/skel/." "$root$home/"
printf '%s:x:0:0::%s:/bin/bash\n' "$login" "$home" >>"$root/etc/passwd"
EOF
  chmod 755 "$fake_bin/useradd"
  user_target="$test_tmp/user-apply-root"
  mkdir -p "$user_target/etc" "$user_target/usr" "$user_target/var"
  printf 'root:x:0:0:root:/root:/bin/bash\n' >"$user_target/etc/passwd"
  printf 'root:x:0:\n' >"$user_target/etc/group"
  user_output=$(unshare --user --map-root-user env PATH="$fake_bin:$PATH" bash -c '
    umask 077
    bash "$1" --rootfs "$2" --source-checkout "$3" --user desktop --home /home/desktop
  ' _ "$ROOT/install/arm64/provision-desktop-root.sh" "$user_target" "$test_checkout" 2>&1) ||
    fail "target user provisioning applies in a disposable root" "$user_output"
  for path in \
    "$user_target/home/desktop/.config" \
    "$user_target/home/desktop/.config/uwsm/env.d" \
    "$user_target/home/desktop/.config/systemd/user/graphical-session.target.wants" \
    "$user_target/home/desktop/.local/share/omarchy-pi" \
    "$user_target/home/desktop/.local/share/omarchy-pi/releases"; do
    assert_mode 755 "$path"
  done
  grep -Fq '$HOME/.local/bin' \
    "$user_target/home/desktop/.config/uwsm/env.d/90-omarchy-pi" ||
    fail "target user UWSM environment includes the user bin directory"
  pass "target-user fixture keeps managed paths traversable under umask 077"
else
  skip "generic umask regression (user namespaces unavailable)"
fi

user_dry_run_output=$(bash "$ROOT/install/arm64/provision-desktop-root.sh" \
  --rootfs "$target" --source-checkout "$test_checkout" --user desktop --home /home/desktop --dry-run)
grep -Fq 'create target account desktop' <<<"$user_dry_run_output" || fail "user dry-run plans target account creation"
grep -Fq 'copy Omarchy release' <<<"$user_dry_run_output" || fail "user dry-run plans a versioned release"
grep -Fq 'graphical-session.target.wants/omarchy-pi-hypr-rdp.service' <<<"$user_dry_run_output" ||
  fail "user dry-run plans target-local RDP unit enablement"
grep -Fq '/etc/systemd/user/omarchy-pi-hypr-rdp.service' <<<"$user_dry_run_output" ||
  fail "user dry-run keeps RDP unit system-owned"
grep -Fq 'create directory '"$target"'/home/desktop/.config' <<<"$user_dry_run_output" ||
  fail "user dry-run plans an owned config directory"
grep -Fq 'create directory '"$target"'/home/desktop/.local/share/omarchy-pi/releases' <<<"$user_dry_run_output" ||
  fail "user dry-run plans an owned release directory"
[[ ! -e $target/home ]] || fail "user dry-run does not create a home"
pass "user provisioning dry-run keeps account creation target-local"

source_text=$(<"$ROOT/config/hypr/hyprland.lua")
grep -Fq 'require("hypr.monitors")' <<<"$source_text" || fail "fresh config uses upstream Hyprland modules"
prefix_text=$(<"$ROOT/install/arm64/session/fresh-hyprland-prefix.lua")
grep -Fq '_G.omarchy_autostart_minimal = true' <<<"$prefix_text" || fail "fresh config defers unsupported system autostart"
if grep -Fq 'append_headless_hook' "$ROOT/install/arm64/provision-desktop-root.sh" ||
  grep -Fq 'headless-output compatibility hook' "$ROOT/install/arm64/provision-desktop-root.sh"; then
  fail "fresh setup must leave headless output creation to guarded start-shell"
fi
if grep -Fq 'OMARCHY_PI_MINIMAL_SESSION' "$ROOT/install/arm64/provision-desktop-root.sh" ||
  grep -Fq '_G.omarchy_default_bindings = false' <<<"$prefix_text"; then
  fail "fresh provisioner must not force the reduced session profile"
fi
grep -Fq 'pam_faillock.so preauth' "$ROOT/install/arm64/session/omarchy-lock-password" ||
  fail "fresh target includes lock password PAM preauth"
grep -Fq 'account    include                     system-local-login' \
  "$ROOT/install/arm64/session/omarchy-lock-password" ||
  fail "fresh target includes lock password account policy"
expected_pam=$(awk '
  /as_root tee \/etc\/pam\.d\/omarchy-lock-password/ { capture=1; next }
  capture && /^EOF$/ { exit }
  capture { print }
' "$ROOT/bin/omarchy-apply-lock")
actual_pam=$(<"$ROOT/install/arm64/session/omarchy-lock-password")
[[ $actual_pam == "$expected_pam" ]] || fail "fresh PAM asset matches upstream lock policy"
pass "fresh provisioning keeps upstream bindings and lock authentication"
