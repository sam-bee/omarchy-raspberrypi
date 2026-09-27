# First-session smoke commands

These blocks are an operator runbook, not an unattended installer. Set the variables from the private run record before use. The target account must already exist. Do not place a password, Wi-Fi secret, SSH key, recovery archive, host identifier or package evidence in this repository.

```bash
set -euo pipefail
umask 077
: "${PI_HOST:?set the target SSH host in the private run record}"
: "${PI_USER:?set the target account in the private run record}"
: "${PI_HOME:?set the target home directory in the private run record}"
: "${PI_UID:?set the target numeric UID in the private run record}"
: "${PI_GID:?set the target numeric GID in the private run record}"
: "${PI_VT:?set an unused local VT for the bounded test}"
PI_RUNTIME_DIR="/run/user/$PI_UID"
PI_RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
```

## Preflight and private backup

Keep two independent SSH connections open. From the target, collect the package inventory, account and group files, enabled-unit state, mounts, boot and encryption metadata, network state and protected-file hashes into a mode-0700 private evidence directory. Include an encrypted, independently testable recovery copy before installing anything. Do not print or upload the contents of credential files, key material, Wi-Fi configuration or recovery archives.

```bash
PI_EVIDENCE="$PI_HOME/.local/state/omarchy-pi-first-session/$PI_RUN_ID"
install -d -m 0700 "$PI_EVIDENCE"
pacman -Q > "$PI_EVIDENCE/packages.before"
pacman -Qqe > "$PI_EVIDENCE/packages.explicit.before"
id "$PI_USER" > "$PI_EVIDENCE/account.before"
systemctl list-unit-files --state=enabled --no-pager > "$PI_EVIDENCE/system-units.enabled.before"
systemctl is-active sshd systemd-networkd || true
findmnt --real > "$PI_EVIDENCE/mounts.before"
lsblk --fs > "$PI_EVIDENCE/block-devices.before"
ip -br address > "$PI_EVIDENCE/addresses.before"
ip route show > "$PI_EVIDENCE/routes.before"
sudo awk '{print $1}' /proc/sys/kernel/random/boot_id > "$PI_EVIDENCE/boot-id.before"
sudo cryptsetup status --all > "$PI_EVIDENCE/encryption.before" || true
sudo sha256sum /etc/passwd /etc/shadow /etc/group /etc/gshadow > "$PI_EVIDENCE/accounts.sha256.before"
```

Confirm from the workstation that a fresh, independent SSH connection succeeds with the target's verified host key. Keep the endpoint and host-key details in the private record; use the variables above in any command example:

```bash
ssh -F /dev/null -o StrictHostKeyChecking=yes "$PI_USER@$PI_HOST" 'id && findmnt --real && systemctl is-active sshd systemd-networkd'
```

## Package gate

Resolve the complete package closure in a separate copy of the package database and trusted keyring. Record the exact manifest and archive/signature hashes privately. Download and verify every archive before the transaction. Review archive paths, install scripts, hooks, sysusers/tmpfiles declarations and service presets. The final interactive transaction must contain only the approved additions and explicitly reviewed existing-package changes.

Do not refresh the live package database as part of an unrelated probe. Do not use `--nodeps`, `--overwrite`, signature bypasses, hook suppression or recursive orphan removal. Reject any unreviewed change to the kernel, firmware, initramfs, bootloader, storage, encryption, network, SSH or firewall configuration.

After the transaction, compare actual package names, versions and install reasons with the private manifest. Recheck protected-file hashes, boot and encryption metadata, enabled units, mounts, routes, firewall state and a new independent SSH login. Package-owned units may be installed while remaining disabled and inactive; verify that state explicitly.

## Temporary session configuration

Use a namespaced directory under the target home. Refuse existing destinations, including dangling symlinks, and install only the configuration required for this smoke test. The source files should come from the reviewed private run record or from the corresponding generic runtime template, never from a machine-specific working copy.

```bash
PI_SMOKE_DIR="$PI_HOME/.config/omarchy-pi-smoke"
[[ ! -e $PI_SMOKE_DIR && ! -L $PI_SMOKE_DIR ]]
install -d -m 0700 "$PI_SMOKE_DIR"
# Copy the reviewed temporary Hyprland and Foot files here, then verify their hashes.
chown "$PI_UID:$PI_GID" "$PI_SMOKE_DIR"
chmod 0700 "$PI_SMOKE_DIR"
sudo -u "$PI_USER" Hyprland --verify-config --config "$PI_SMOKE_DIR/hyprland.lua"
```

Do not create a display-manager configuration, shell-profile change, persistent unit, automatic-login rule or general `~/.config/hypr` replacement for this test.

## Local VT session

From the target, confirm that the chosen VT and logind seat are unused and that no compositor, Wayland socket or test unit is active. Record the current foreground VT before switching. A second SSH connection must remain available for recovery.

```bash
export XDG_RUNTIME_DIR="$PI_RUNTIME_DIR"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$PI_RUNTIME_DIR/bus"
[[ -S $XDG_RUNTIME_DIR/bus ]]
loginctl list-sessions
loginctl seat-status seat0
systemctl show -p LoadState -p ActiveState "getty@tty$PI_VT.service" omarchy-pi-smoke.service
systemctl --user list-units --all 'wayland-wm@*.service' 'graphical-session*.target' --no-pager
find "$XDG_RUNTIME_DIR" -maxdepth 1 -type s -name 'wayland-*' -print
PI_SAVED_VT=$(sudo fgconsole)
[[ $PI_SAVED_VT =~ ^[0-9]+$ ]]
```

Stop if a different local session, compositor, Wayland socket or unit is present. Do not reset the account's whole user manager or terminate another user's session to make the gate pass.

Set a cleanup trap before changing the VT. Record the new session ID, compositor PID and instance signature only after inspecting `loginctl` and `hyprctl` output. The cleanup must restore the saved VT first, stop only the named service and matching compositor unit, terminate only the matching test PAM session, and confirm that the client and socket are gone.

```bash
set -euo pipefail
PI_UNIT=omarchy-pi-smoke.service
PI_SESSION_ID=
PI_SESSION_LEADER=
PI_WM_PID=
PI_SIGNATURE=
PI_LAUNCH_AT=$(date -u --iso-8601=seconds)
PI_SMOKE_EVIDENCE="$PI_EVIDENCE/session"
install -d -m 0700 "$PI_SMOKE_EVIDENCE"

cleanup() {
  local failed=0 current_pid
  trap - INT TERM HUP
  sudo -n chvt "$PI_SAVED_VT" || failed=1
  sudo -n journalctl -u "$PI_UNIT" --since "$PI_LAUNCH_AT" --no-pager > "$PI_SMOKE_EVIDENCE/unit.log" || failed=1
  if [[ -n $PI_SIGNATURE && -f "$PI_RUNTIME_DIR/hypr/$PI_SIGNATURE/hyprland.log" ]]; then
    cp "$PI_RUNTIME_DIR/hypr/$PI_SIGNATURE/hyprland.log" "$PI_SMOKE_EVIDENCE/hyprland.log" || failed=1
  fi
  if [[ -n $PI_WM_PID ]]; then
    current_pid=$(systemctl --user show -p MainPID --value wayland-wm@Hyprland.service) || failed=1
    if [[ $current_pid == "$PI_WM_PID" ]]; then
      systemctl --user stop wayland-wm@Hyprland.service || failed=1
    elif [[ $current_pid != 0 ]]; then
      failed=1
    fi
  fi
  if [[ $(systemctl show -p LoadState --value "$PI_UNIT") != not-found ]]; then
    sudo -n systemctl stop "$PI_UNIT" || failed=1
  fi
  if [[ -n $PI_SESSION_ID ]] && loginctl show-session "$PI_SESSION_ID" >/dev/null 2>&1; then
    if [[ $(loginctl show-session "$PI_SESSION_ID" -p Name --value) == "$PI_USER" && $(loginctl show-session "$PI_SESSION_ID" -p Remote --value) == no && $(loginctl show-session "$PI_SESSION_ID" -p Seat --value) == seat0 && $(loginctl show-session "$PI_SESSION_ID" -p VTNr --value) == "$PI_VT" && $(loginctl show-session "$PI_SESSION_ID" -p Leader --value) == "$PI_SESSION_LEADER" ]]; then
      sudo -n loginctl terminate-session "$PI_SESSION_ID" || failed=1
    else
      failed=1
    fi
  fi
  (( failed == 0 )) || echo 'Inspect cleanup from the recovery connection' >&2
  (( failed == 0 ))
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

sudo -v
sudo chvt "$PI_VT"
env -u OMARCHY_PATH sudo systemd-run --unit="$PI_UNIT" --collect --service-type=exec \
  --property="User=$PI_USER" --property=PAMName=login \
  --property="WorkingDirectory=$PI_HOME" \
  --property="TTYPath=/dev/tty$PI_VT" --property=StandardInput=tty \
  --property=StandardOutput=journal --property=StandardError=journal \
  --property=TTYReset=yes --property=TTYVHangup=yes \
  --property=Restart=no --property=RuntimeMaxSec=5min \
  --property=TimeoutStopSec=15s --property=KillMode=control-group \
  --setenv="HOME=$PI_HOME" --setenv="XDG_RUNTIME_DIR=$PI_RUNTIME_DIR" \
  --setenv="DBUS_SESSION_BUS_ADDRESS=unix:path=$PI_RUNTIME_DIR/bus" \
  --setenv=XDG_SESSION_TYPE=wayland --setenv=XDG_SESSION_DESKTOP=Hyprland \
  --setenv=XDG_CURRENT_DESKTOP=Hyprland --setenv=AQ_NO_KMS_REQUIREMENT=1 \
  /usr/bin/uwsm start -g -1 -U run -e -D Hyprland -- \
  /usr/bin/Hyprland --config "$PI_SMOKE_DIR/hyprland.lua"
```

Immediately identify the one new local session and compositor instance. Verify that the account, seat, VT, PID and configuration path match this run before issuing any `hyprctl` command. If no monitor is connected, create one headless output through the identified instance. Launch Foot through that instance and record the output/client JSON privately.

```bash
loginctl list-sessions
# Set PI_SESSION_ID, PI_SESSION_LEADER and PI_SIGNATURE only after inspection.
loginctl show-session "$PI_SESSION_ID" -p Name -p Remote -p Seat -p VTNr -p Active -p Leader
systemctl --user show wayland-wm@Hyprland.service -p ActiveState -p MainPID
hyprctl instances -j
export HYPRLAND_INSTANCE_SIGNATURE="$PI_SIGNATURE"
hyprctl configerrors
hyprctl -j monitors
hyprctl -j clients
```

## Postcheck and rollback

After cleanup, require the saved VT, no test service, no test PAM session, no compositor/client process, no test Wayland socket and no unexpected graphical user environment. Repeat the package, account, mount, boot, encryption, network, service and protected-file checks from the private record. Remove only the temporary namespaced configuration and the reviewed package additions if the run is being rolled back. Never restore a saved package database over live files or use a recursive package removal to recover from an incomplete test.
