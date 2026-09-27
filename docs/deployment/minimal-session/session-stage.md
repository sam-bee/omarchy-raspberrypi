# Bounded Pi UWSM and Quickshell session

This runbook checks the minimal Omarchy session after the [package gate](package-stage.md) has passed. It starts one local logind session on an unused VT, launches the staged UWSM/Hyprland session, checks the minimal shell and a terminal client, captures a frame when the test environment supports it, then cleans up. It does not enable a unit, change the default target, configure automatic login, install a display manager, reboot or perform a package update.

Set the target account, home directory, UID, runtime directory, SSH endpoint and source revision from the private run record. Keep two independent recovery connections open. Do not copy recovery material or a machine-specific checkout into this repository.

```bash
set -euo pipefail
umask 077
: "${PI_HOST:?set the target SSH host in the private run record}"
: "${PI_USER:?set the target account in the private run record}"
: "${PI_HOME:?set the target home directory in the private run record}"
: "${PI_UID:?set the target numeric UID in the private run record}"
: "${PI_VT:?set an unused local VT for the bounded test}"
PI_RUNTIME_DIR="/run/user/$PI_UID"
PI_RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
```

## Source and staging gates

Use a clean checkout of the reviewed source revision. Verify the revision and working tree before staging; do not follow a moving branch. The account must own its home and destination parents, and each managed destination must be absent, including dangling symlinks.

```bash
[[ -d $PI_HOME && ! -L $PI_HOME ]]
[[ $(stat -c %u "$PI_HOME") == "$PI_UID" ]]
[[ -z ${XDG_CONFIG_HOME:-} || ${XDG_CONFIG_HOME%/} == "$PI_HOME/.config" ]]
```

Run the approved session staging command and verify only the managed configuration files, release pointer and source marker. Inspect the resulting release manifest privately. A distributable release must not include deployment evidence, credentials, host identifiers or machine-specific documentation.

```bash
source "$PI_HOME/.config/uwsm/env.d/90-omarchy-pi"
[[ $OMARCHY_PATH == "$PI_HOME/.local/share/omarchy-pi/current" ]]
[[ $OMARCHY_PI_MINIMAL_SESSION == 1 ]]
[[ $(xdg-terminal-exec --print-id) == foot.desktop ]]
Hyprland --verify-config --config "$PI_HOME/.config/hypr/hyprland.lua"
uwsm start -n -g -1 -U run -e -D Hyprland -- /usr/bin/Hyprland --config "$PI_HOME/.config/hypr/hyprland.lua"
```

Before a real start, inspect enabled graphical user units and XDG autostarts. Stop if an unexpected application, prior graphical target, compositor, Wayland socket or UWSM-generated unit would be started. Do not reset the whole user manager to clear a stale environment; record and clear only variables left by this bounded test after confirming no session is active.

## Local session gate

Confirm that the chosen VT and seat are unused, `getty` is inactive, no named test unit exists and the user D-Bus socket is available. Record the current foreground VT immediately before switching. A recovery connection must remain usable.

```bash
export XDG_RUNTIME_DIR="$PI_RUNTIME_DIR"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$PI_RUNTIME_DIR/bus"
[[ -S $XDG_RUNTIME_DIR/bus ]]
loginctl list-sessions
loginctl seat-status seat0
systemctl show -p LoadState -p ActiveState "getty@tty$PI_VT.service" omarchy-pi-uwsm-smoke.service
systemctl --user list-units --all 'wayland-wm@*.service' 'graphical-session*.target' --no-pager
find "$XDG_RUNTIME_DIR" -maxdepth 1 -type s -name 'wayland-*' -print
PI_SAVED_VT=$(sudo fgconsole)
[[ $PI_SAVED_VT =~ ^[0-9]+$ ]]
```

If a local user session, graphical unit, compositor, socket or other operator is present, stop and investigate. Never terminate another session to make the gate pass.

## Bounded launch and checks

Use a named transient system service with explicit `User`, `PAMName`, `TTYPath`, `WorkingDirectory`, runtime limit and environment. Set a cleanup trap before switching VTs. Record only the new session ID, matching leader, compositor unit/PID and instance signature after inspecting command output. Do not pass a host-specific `OMARCHY_PATH` through the transient service; the session environment must come from the managed file.

```bash
PI_UNIT=omarchy-pi-uwsm-smoke.service
PI_LAUNCH_AT=$(date -u --iso-8601=seconds)
PI_SESSION_ID=
PI_SESSION_LEADER=
PI_WM_PID=
PI_SIGNATURE=
PI_EVIDENCE="$PI_HOME/.local/state/omarchy-pi-session/$PI_RUN_ID"
install -d -m 0700 "$PI_EVIDENCE"

cleanup() {
  local failed=0 current_pid
  trap - INT TERM HUP
  sudo -n chvt "$PI_SAVED_VT" || failed=1
  sudo -n journalctl -u "$PI_UNIT" --since "$PI_LAUNCH_AT" --no-pager > "$PI_EVIDENCE/transient-unit.log" || failed=1
  sudo -n journalctl -t omarchy-shell --since "$PI_LAUNCH_AT" --no-pager > "$PI_EVIDENCE/omarchy-shell.log" || failed=1
  if [[ -n $PI_SIGNATURE && -f "$PI_RUNTIME_DIR/hypr/$PI_SIGNATURE/hyprland.log" ]]; then
    cp "$PI_RUNTIME_DIR/hypr/$PI_SIGNATURE/hyprland.log" "$PI_EVIDENCE/hyprland.log" || failed=1
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
env -u OMARCHY_PATH -u OMARCHY_PI_MINIMAL_SESSION sudo systemd-run --unit="$PI_UNIT" --collect --service-type=exec \
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
  /usr/bin/start-hyprland -- --config "$PI_HOME/.config/hypr/hyprland.lua"
```

Inspect the new session and compositor before filling the placeholders below. Require one active local session with the chosen user, seat and VT, one matching compositor child, the staged release path and minimal-session flag in the user manager and compositor environment, and no configuration errors.

```bash
loginctl list-sessions
# Set PI_SESSION_ID, PI_SESSION_LEADER, PI_WM_PID and PI_SIGNATURE only after inspection.
loginctl show-session "$PI_SESSION_ID" -p Name -p Remote -p Seat -p VTNr -p Active -p Leader
systemctl --user show wayland-wm@Hyprland.service -p ActiveState -p MainPID
hyprctl instances -j
export HYPRLAND_INSTANCE_SIGNATURE="$PI_SIGNATURE"
systemctl --user show-environment | grep -E '^(OMARCHY_PATH|OMARCHY_PI_MINIMAL_SESSION|WAYLAND_DISPLAY)='
tr '\0' '\n' < "/proc/$PI_WM_PID/environ" | grep -E '^(OMARCHY_PATH|OMARCHY_PI_MINIMAL_SESSION)='
hyprctl configerrors
hyprctl -j monitors
hyprctl -j clients
```

If no monitor is connected, create one headless output through the identified compositor instance and then launch Foot through that same instance. Use `grim` only with the verified `WAYLAND_DISPLAY`, and retain any frame outside the public repository. A successful compositor/client probe without a captured frame is not visual acceptance.

## Cleanup and acceptance

The cleanup must restore the saved VT, stop the named units, terminate only the recorded PAM session, and confirm that the compositor, shell, terminal, clients and Wayland socket have exited. Repeat the package, account, mount, boot, encryption, network, service and protected-file checks from the private baseline. Record results privately, including source revision, package manifest, process identities, hashes and any frame.

If a check fails, stop and preserve evidence. Do not reboot, enable the session at boot, kill the whole user manager, remove unrelated packages or restore a whole system archive over a live installation. A separate controlled reboot and persistent-startup review is required before enabling any boot-time session.
