# Bounded Pi UWSM and Quickshell session

**Status: validated on 23 September 2026.** [The session result](session-result.md) records three direct-Hyprland attempts and one `start-hyprland` wrapper attempt. The 32-package transaction in [the package stage](package-stage.md) is separate and must pass its preservation gate before this runbook is used. The earlier [Hyprland and Foot smoke](../first-session/smoke-result.md) proved a local tty8 seat and Pi V3D rendering, but needed an explicit headless output and did not capture a frame. This experiment tests the staged minimal Omarchy bar in a five-minute local session. It does not install packages, enable a unit, change the default target, install a display manager, configure autologin, or reboot. The four new user configuration files and staged source release now exist on this Pi; the first-use staging commands below must not be rerun over them.

Use two independent SSH connections to `sierra@192.168.178.21`; keep one available for recovery. Run each block, inspect its output, and proceed only when its stated conditions hold. Commands labeled **workstation** run in the main `quattro-rpi5` checkout; other commands run as Sierra on the Pi. The private package baseline path and source revision are operator inputs, not values to guess. Do not print or transfer recovery keys, credentials, or the unencrypted baseline. The commands below record the validated direct-Hyprland experiment; the tested wrapper changes the unit and process identity as described at the end.

## 1. Package and source gates

Require the [package-stage postchecks](package-stage.md#transaction-gate-and-bounded-procedure) first: exactly 329 installed packages, the six reviewed roots and 26 dependencies, no changed old version, `pacman -Dk` clean, and the reviewed Avahi identity additions only. Confirm protected boot/encryption/network state and a fresh SSH login against the new post-smoke backup before staging files. In particular, check `fumon.service` in the **user** manager, and Avahi in the system manager; both must remain inactive and disabled.

```bash
pacman -Q uwsm quickshell xdg-terminal-exec inotify-tools qt6-wayland grim
pacman -Dk
systemctl --user show -p UnitFileState -p ActiveState fumon.service
systemctl show -p UnitFileState -p ActiveState avahi-daemon.service avahi-daemon.socket avahi-dnsconfd.service
systemctl is-active sshd systemd-networkd wpa_supplicant@wld0
```

Compare the six versions with [the frozen manifest](packages-2026-09-23.tsv). Stop if `fumon` or any Avahi unit is enabled or active, if the preservation review is incomplete, or if an unrelated graphical user unit is already active. Keep the package backup and its verified encrypted off-Pi copy; this session does not change their recovery procedure.

**Workstation:** require the clean, reviewed main commit to be the exact commit published on the public implementation branch. Transfer only that revision identifier to the Pi; the Pi fetches a shallow checkout over HTTPS. A moving remote branch is caught by the exact post-clone comparison. If HTTPS cloning fails on the Pi, stop and arrange a separately reviewed source transfer instead of staging another revision.

```bash
[[ $(git branch --show-current) == "quattro-rpi5" ]]
[[ -z $(git status --porcelain --untracked-files=all) ]]
PI_SOURCE_REV=$(git rev-parse --verify HEAD)
PI_REMOTE_REV=$(git ls-remote https://github.com/sam-bee/omarchy-raspberrypi.git refs/heads/quattro-rpi5 | cut -f1)
[[ $PI_REMOTE_REV == "$PI_SOURCE_REV" ]]
PI_TRANSFER_TMP=$(mktemp -d)
printf '%s\n' "$PI_SOURCE_REV" > "$PI_TRANSFER_TMP/source.revision"
(cd "$PI_TRANSFER_TMP" && sha256sum source.revision > transfer.sha256)
ssh -F /dev/null -o StrictHostKeyChecking=yes sierra@192.168.178.21 'umask 077; mkdir -p "$HOME/.local/state/omarchy-pi-transfer"; chmod 0700 "$HOME/.local/state/omarchy-pi-transfer"'
scp -F /dev/null -o StrictHostKeyChecking=yes "$PI_TRANSFER_TMP/source.revision" "$PI_TRANSFER_TMP/transfer.sha256" sierra@192.168.178.21:/home/sierra/.local/state/omarchy-pi-transfer/
```

**Pi:** verify the revision file, create a new shallow clean checkout, and require the exact reviewed commit. A failed clone or mismatch is a stop; do not use an existing dirty checkout. Keep this checkout until the session evidence has been reviewed.

```bash
set -euo pipefail
umask 077
[[ $HOME == "/home/sierra" && $(id -u) == "1000" ]]
PI_TRANSFER="$HOME/.local/state/omarchy-pi-transfer"
(cd "$PI_TRANSFER" && sha256sum --check transfer.sha256)
PI_SOURCE_REV=$(cat "$PI_TRANSFER/source.revision")
[[ $PI_SOURCE_REV =~ ^[0-9a-f]{40}$ ]]
PI_SOURCE="$HOME/.local/share/omarchy-pi-source-$PI_SOURCE_REV"
[[ ! -e $PI_SOURCE && ! -L $PI_SOURCE ]]
git clone --depth=1 --single-branch --branch quattro-rpi5 https://github.com/sam-bee/omarchy-raspberrypi.git "$PI_SOURCE"
[[ $(git -C "$PI_SOURCE" rev-parse --verify HEAD) == "$PI_SOURCE_REV" ]]
[[ -z $(git -C "$PI_SOURCE" status --porcelain --untracked-files=all) ]]
```

## 2. Stage only new Sierra-owned configuration

The stager refuses existing destinations, including dangling links, and publishes a versioned release under `~/.local/share/omarchy-pi/`. Check the home and every existing destination parent before calling it. `XDG_CONFIG_HOME` must be unset or exactly Sierra's default path. Do not overwrite a user's existing configuration or remove a retained partial release to force a retry.

```bash
[[ -z ${XDG_CONFIG_HOME:-} || ${XDG_CONFIG_HOME%/} == "$HOME/.config" ]]
[[ -d $HOME && ! -L $HOME && $(stat -c %u "$HOME") == "$(id -u)" ]]
for parent in "$HOME/.config" "$HOME/.config/uwsm" "$HOME/.config/uwsm/env.d" "$HOME/.config/hypr" "$HOME/.config/omarchy" "$HOME/.local" "$HOME/.local/share" "$HOME/.local/share/omarchy-pi" "$HOME/.local/share/omarchy-pi/releases"; do
  if [[ -e $parent || -L $parent ]]; then
    [[ -d $parent && ! -L $parent && $(stat -c %u "$parent") == "$(id -u)" ]]
  fi
done
for target in "$HOME/.config/uwsm/env.d/90-omarchy-pi" "$HOME/.config/hypr/hyprland.lua" "$HOME/.config/omarchy/shell.json" "$HOME/.config/xdg-terminals.list" "$HOME/.local/share/omarchy-pi/current" "$HOME/.local/share/omarchy-pi/releases/$PI_SOURCE_REV"; do
  [[ ! -e $target && ! -L $target ]]
done
"$PI_SOURCE/install/arm64/stage-user-session.sh"
PI_RELEASE="$HOME/.local/share/omarchy-pi/releases/$PI_SOURCE_REV"
[[ $(cat "$PI_RELEASE/.omarchy-pi-source-commit") == "$PI_SOURCE_REV" ]]
[[ -L $HOME/.local/share/omarchy-pi/current && $(readlink "$HOME/.local/share/omarchy-pi/current") == "releases/$PI_SOURCE_REV" ]]
cmp "$PI_RELEASE/install/arm64/session/90-omarchy-pi" "$HOME/.config/uwsm/env.d/90-omarchy-pi"
cmp "$PI_RELEASE/install/arm64/session/hyprland.lua" "$HOME/.config/hypr/hyprland.lua"
cmp "$PI_RELEASE/install/arm64/session/shell.json" "$HOME/.config/omarchy/shell.json"
cmp "$PI_RELEASE/install/arm64/session/xdg-terminals.list" "$HOME/.config/xdg-terminals.list"
```

Source the staged UWSM environment in this SSH shell for parse probes only. It must point at the staged release and select Foot. Confirm that the command's dry run prints the intended Hyprland config and `Hyprland` compositor ID. UWSM 0.27's `-n` is documented in its [tagged source](https://github.com/Vladimir-csp/uwsm/blob/v0.27.0/uwsm/main.py) as no-write/no-start; it is still a probe on this Pi.

```bash
source "$HOME/.config/uwsm/env.d/90-omarchy-pi"
[[ $OMARCHY_PATH == "$HOME/.local/share/omarchy-pi/current" ]]
[[ $OMARCHY_PI_MINIMAL_SESSION == "1" ]]
[[ $PATH == "$OMARCHY_PATH/bin:"* ]]
[[ $(xdg-terminal-exec --print-id) == "foot.desktop" ]]
Hyprland --verify-config --config "$HOME/.config/hypr/hyprland.lua"
uwsm start -n -g -1 -U run -e -D Hyprland -- /usr/bin/Hyprland --config /home/sierra/.config/hypr/hyprland.lua
```

UWSM `-U run` selects runtime-only generated drop-ins, but UWSM may remove its own older managed files in the other, home rung. Before a real start, require no pre-existing UWSM-managed `wayland-wm@*`, `wayland-wm-env@*`, or UWSM tweak drop-ins in `~/.config/systemd/user/`; inspect and stop if present. Also inspect Sierra's enabled graphical user units and XDG autostarts. UWSM may start those when the graphical session target is reached; do not launch if an unexpected application or service would start.

```bash
if [[ -d $HOME/.config/systemd/user ]]; then
  find "$HOME/.config/systemd/user" -maxdepth 4 \( -name '*wayland-wm*' -o -name '*wayland-session*' -o -name 'slice-tweak.conf' \) -print
fi
systemctl --user list-unit-files --state=enabled --no-pager
for directory in "$HOME/.config/autostart" /etc/xdg/autostart /usr/share/xdg/autostart /usr/share/autostart; do
  if [[ -d $directory ]]; then
    find "$directory" -maxdepth 1 -type f -name '*.desktop' -print
  fi
done
```

## 3. Local tty8 preflight and transient launch

Use a dedicated interactive Bash shell for the experiment; the cleanup traps below belong to that shell. Do not use `uwsm check may-start` as a tty8 gate: its default policy expects a login shell on foreground tty1. Establish the actual prestate and keep a second SSH connection open. Confirm `getty@tty8` inactive, no seat0 user session, no active graphical user target/compositor, no prior named unit, and a working user D-Bus socket. Record the current foreground VT immediately before switching.

```bash
export XDG_RUNTIME_DIR=/run/user/1000
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
[[ -S $XDG_RUNTIME_DIR/bus ]]
loginctl list-sessions
loginctl seat-status seat0
systemctl show -p LoadState -p ActiveState getty@tty8.service omarchy-pi-uwsm-smoke.service
systemctl --user list-units --all 'wayland-wm@*.service' 'graphical-session*.target' --no-pager
systemctl --user list-unit-files --state=enabled --no-pager
systemctl --user show -p UnitFileState -p ActiveState fumon.service
systemctl --user show-environment | grep -E '^(WAYLAND_DISPLAY|XDG_CURRENT_DESKTOP|OMARCHY_PATH|OMARCHY_PI_MINIMAL_SESSION)=' || true
find "$XDG_RUNTIME_DIR" -maxdepth 1 -type s -name 'wayland-*' -print
sudo fgconsole
```

Stop if any of those gates differs from the reviewed prestate. A read-only preflight after the previous smoke found stale `WAYLAND_DISPLAY=wayland-1` and `XDG_CURRENT_DESKTOP=Hyprland` in Sierra's user manager, although no compositor was active. The first launch cleared those exact values after confirming there was no local graphics session or Wayland socket. Subsequent preflights found all four session variables absent. A different value, any existing `OMARCHY_PATH` or minimal flag, or a live socket is a stop. Postcleanup requires the four session variables absent. `systemd-run` with `PAMName=login`, a controlling tty8, and `User=sierra` produced an active local Sierra seat in both UWSM variants. The Pi's observed default is `graphical.target`; `-g -1` disabled UWSM's graphical-target wait for this bounded experiment without changing the default. The five-minute service limit is a backstop, not proof that UWSM's separate user units and PAM session have stopped.

In the dedicated Bash shell, set the trap before `chvt`. Fill `PI_SESSION_ID`, `PI_SESSION_LEADER`, `PI_WM_UNIT`, and `PI_WM_PID` only after checking their identities below. The trap restores the VT first, stops only the recorded compositor unit if its PID still matches, then stops the named transient service. It terminates a remaining PAM session only after matching all recorded local tty8 properties. If any cleanup check fails, use the second SSH connection to inspect the state; never terminate Sierra's whole user manager or an SSH session.

```bash
set -euo pipefail
umask 077
PI_UNIT=omarchy-pi-uwsm-smoke.service
PI_SAVED_VT=$(sudo fgconsole)
[[ $PI_SAVED_VT =~ ^[0-9]+$ ]]
PI_SESSION_ID=
PI_SESSION_LEADER=
PI_WM_UNIT=
PI_WM_PID=
PI_SIGNATURE=
PI_LAUNCH_AT=$(date -u --iso-8601=seconds)
PI_RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
PI_EVIDENCE="$HOME/.local/state/omarchy-pi-session/$PI_RUN_ID"
install -d -m 0700 "$PI_EVIDENCE"
systemctl --user list-unit-files --state=enabled --no-legend --no-pager > "$PI_EVIDENCE/user-enabled.before"
sudo sha256sum /etc/passwd /etc/shadow /etc/group /etc/gshadow > "$PI_EVIDENCE/account-files.before.sha256"
systemctl --user show-environment | grep -E '^(WAYLAND_DISPLAY|XDG_CURRENT_DESKTOP|OMARCHY_PATH|OMARCHY_PI_MINIMAL_SESSION)=' > "$PI_EVIDENCE/graphical-env.original" || true
if [[ -s $PI_EVIDENCE/graphical-env.original ]]; then
  grep -Fxq 'WAYLAND_DISPLAY=wayland-1' "$PI_EVIDENCE/graphical-env.original"
  grep -Fxq 'XDG_CURRENT_DESKTOP=Hyprland' "$PI_EVIDENCE/graphical-env.original"
  [[ $(wc -l < "$PI_EVIDENCE/graphical-env.original") == 2 ]]
fi
[[ ! -S $XDG_RUNTIME_DIR/wayland-1 ]]
if find "$XDG_RUNTIME_DIR" -maxdepth 1 -type s -name 'wayland-*' -print -quit | grep -q .; then
  echo 'A Wayland socket exists; do not reset the user environment or launch' >&2
  exit 1
fi
if [[ -s $PI_EVIDENCE/graphical-env.original ]]; then
  systemctl --user unset-environment WAYLAND_DISPLAY XDG_CURRENT_DESKTOP
fi
if systemctl --user show-environment | grep -Eq '^(WAYLAND_DISPLAY|XDG_CURRENT_DESKTOP)='; then
  echo 'Stale graphical environment remains; do not launch' >&2
  exit 1
fi
pi_session_cleanup() {
  local failed=0 current_pid
  trap - INT TERM HUP
  sudo -n chvt "$PI_SAVED_VT" || failed=1
  sudo -n journalctl -u "$PI_UNIT" --since "$PI_LAUNCH_AT" --no-pager > "$PI_EVIDENCE/transient-unit.log" || failed=1
  sudo -n journalctl -t omarchy-shell --since "$PI_LAUNCH_AT" --no-pager > "$PI_EVIDENCE/omarchy-shell.log" || failed=1
  if [[ -n $PI_WM_UNIT ]]; then
    journalctl --user -u "$PI_WM_UNIT" --since "$PI_LAUNCH_AT" --no-pager > "$PI_EVIDENCE/uwsm-compositor.log" || failed=1
  fi
  if [[ -n $PI_SIGNATURE && -f /run/user/1000/hypr/$PI_SIGNATURE/hyprland.log ]]; then
    cp "/run/user/1000/hypr/$PI_SIGNATURE/hyprland.log" "$PI_EVIDENCE/hyprland.log" || failed=1
  fi
  if [[ -n $PI_WM_UNIT && -n $PI_WM_PID ]]; then
    current_pid=$(systemctl --user show -p MainPID --value "$PI_WM_UNIT") || failed=1
    if [[ $current_pid == "$PI_WM_PID" ]]; then
      systemctl --user stop "$PI_WM_UNIT" || failed=1
    elif [[ $current_pid != "0" ]]; then
      echo "Compositor PID changed; inspect before stopping it" >&2
      failed=1
    fi
  fi
  if [[ $(systemctl show -p LoadState --value "$PI_UNIT") != "not-found" ]]; then
    sudo -n systemctl stop "$PI_UNIT" || failed=1
  fi
  if [[ -n $PI_SESSION_ID ]] && loginctl show-session "$PI_SESSION_ID" >/dev/null 2>&1; then
    if [[ $(loginctl show-session "$PI_SESSION_ID" -p Name --value) == "sierra" && $(loginctl show-session "$PI_SESSION_ID" -p Remote --value) == "no" && $(loginctl show-session "$PI_SESSION_ID" -p Seat --value) == "seat0" && $(loginctl show-session "$PI_SESSION_ID" -p VTNr --value) == "8" && $(loginctl show-session "$PI_SESSION_ID" -p Leader --value) == "$PI_SESSION_LEADER" ]]; then
      sudo -n loginctl terminate-session "$PI_SESSION_ID" || failed=1
    else
      echo "Session identity changed; inspect before terminating it" >&2
      failed=1
    fi
  fi
  if (( failed == 0 )); then
    trap - EXIT
  else
    echo "Cleanup or evidence capture is incomplete; inspect from the second SSH connection" >&2
  fi
  (( failed == 0 ))
}
trap pi_session_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
sudo -v
sudo chvt 8
env -u OMARCHY_PATH -u OMARCHY_PI_MINIMAL_SESSION sudo systemd-run --unit=omarchy-pi-uwsm-smoke --collect --service-type=exec \
  --property=User=sierra --property=PAMName=login \
  --property=WorkingDirectory=/home/sierra \
  --property=TTYPath=/dev/tty8 --property=StandardInput=tty \
  --property=StandardOutput=journal --property=StandardError=journal \
  --property=TTYReset=yes --property=TTYVHangup=yes \
  --property=Restart=no --property=RuntimeMaxSec=5min \
  --property=TimeoutStopSec=15s --property=KillMode=control-group \
  --setenv=HOME=/home/sierra --setenv=XDG_RUNTIME_DIR=/run/user/1000 \
  --setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
  --setenv=XDG_SESSION_TYPE=wayland --setenv=XDG_SESSION_DESKTOP=Hyprland \
  --setenv=XDG_CURRENT_DESKTOP=Hyprland --setenv=AQ_NO_KMS_REQUIREMENT=1 \
  /usr/bin/uwsm start -g -1 -U run -e -D Hyprland -- \
  /usr/bin/Hyprland --config /home/sierra/.config/hypr/hyprland.lua
source "$HOME/.config/uwsm/env.d/90-omarchy-pi"
[[ $OMARCHY_PATH == "$HOME/.local/share/omarchy-pi/current" && $OMARCHY_PI_MINIMAL_SESSION == "1" ]]
```

Do not pass `OMARCHY_PATH` or `OMARCHY_PI_MINIMAL_SESSION` through `systemd-run`; this test must prove UWSM loaded `~/.config/uwsm/env.d/90-omarchy-pi`. Immediately record the **new** session ID from `loginctl list-sessions`, then set and check the variables below. Inspect the full output before filling the placeholders. If there is more than one new session or compositor, stop and investigate. The unit name `wayland-wm@Hyprland.service` is the expected UWSM ID from the dry run; verify it on the Pi instead of assuming it.

```bash
loginctl list-sessions
PI_SESSION_ID='REPLACE_WITH_NEW_TTY8_SESSION_ID'
[[ $PI_SESSION_ID != "REPLACE_WITH_NEW_TTY8_SESSION_ID" ]]
loginctl show-session "$PI_SESSION_ID" -p Name -p Remote -p Seat -p VTNr -p Active -p Leader
[[ $(loginctl show-session "$PI_SESSION_ID" -p Name --value) == "sierra" ]]
[[ $(loginctl show-session "$PI_SESSION_ID" -p Remote --value) == "no" ]]
[[ $(loginctl show-session "$PI_SESSION_ID" -p Seat --value) == "seat0" ]]
[[ $(loginctl show-session "$PI_SESSION_ID" -p VTNr --value) == "8" ]]
[[ $(loginctl show-session "$PI_SESSION_ID" -p Active --value) == "yes" ]]
PI_SESSION_LEADER=$(loginctl show-session "$PI_SESSION_ID" -p Leader --value)
[[ $PI_SESSION_LEADER =~ ^[1-9][0-9]*$ ]]
PI_WM_UNIT=wayland-wm@Hyprland.service
systemctl --user show "$PI_WM_UNIT" -p ActiveState -p MainPID
[[ $(systemctl --user show "$PI_WM_UNIT" -p ActiveState --value) == "active" ]]
PI_WM_PID=$(systemctl --user show "$PI_WM_UNIT" -p MainPID --value)
[[ $PI_WM_PID =~ ^[1-9][0-9]*$ ]]
hyprctl instances -j
```

Match the single new Hyprland instance PID to `PI_WM_PID`, record its instance signature as `PI_SIGNATURE`, and check that it belongs to this tty8 run. Set `HYPRLAND_INSTANCE_SIGNATURE=$PI_SIGNATURE` in this SSH shell before every `hyprctl` probe. Capture the compositor log from `/run/user/1000/hypr/$PI_SIGNATURE/hyprland.log` before ending the session; the runtime path may disappear during cleanup.

## 4. Environment, bar, terminal, and first frame

The private evidence directory was created before launch, and the cleanup trap collects available journals and the compositor log before stopping the unit. Only query the three named environment variables; do not dump all of `/proc/$PI_WM_PID/environ` or the user manager environment, as other variables may be sensitive. Require the user manager **and** compositor process to have the staged release path and minimal flag. Read `WAYLAND_DISPLAY` from the user manager, confirm its socket, then use that exact display for `omarchy-shell` and `grim` from SSH. Do not select a Wayland socket by modification time when checking this experiment.

```bash
PI_SIGNATURE='REPLACE_WITH_VERIFIED_INSTANCE_SIGNATURE'
[[ $PI_SIGNATURE != "REPLACE_WITH_VERIFIED_INSTANCE_SIGNATURE" ]]
export HYPRLAND_INSTANCE_SIGNATURE="$PI_SIGNATURE"
systemctl --user show-environment | grep -E '^(OMARCHY_PATH|OMARCHY_PI_MINIMAL_SESSION|WAYLAND_DISPLAY)='
tr '\0' '\n' < "/proc/$PI_WM_PID/environ" | grep -E '^(OMARCHY_PATH|OMARCHY_PI_MINIMAL_SESSION)='
PI_WAYLAND_DISPLAY=$(systemctl --user show-environment | sed -n 's/^WAYLAND_DISPLAY=//p')
[[ $PI_WAYLAND_DISPLAY =~ ^wayland-[0-9]+$ ]]
[[ -S $XDG_RUNTIME_DIR/$PI_WAYLAND_DISPLAY ]]
export WAYLAND_DISPLAY="$PI_WAYLAND_DISPLAY"
hyprctl version | tee "$PI_EVIDENCE/hyprland-version.txt"
hyprctl configerrors | tee "$PI_EVIDENCE/config-errors.txt"
python3 - "$PI_EVIDENCE/config-errors.txt" <<'PY'
import pathlib, sys
assert not pathlib.Path(sys.argv[1]).read_text().strip()
PY
for attempt in {1..30}; do
  if hyprctl -j monitors | python3 -c 'import json, sys; sys.exit(not bool(json.load(sys.stdin)))' >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done
hyprctl -j monitors | tee "$PI_EVIDENCE/monitors.json"
hyprctl -j clients | tee "$PI_EVIDENCE/clients-before-foot.json"
for attempt in {1..40}; do
  if omarchy-shell shell ping >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done
omarchy-shell shell ping
mapfile -t PI_QS_PIDS < <(pgrep -u "$(id -u)" -x quickshell)
(( ${#PI_QS_PIDS[@]} == 1 ))
ps -p "${PI_QS_PIDS[0]}" -o pid,comm,args
```

The compositor must report no non-whitespace config errors, a Pi V3D renderer in its log, and exactly one usable output. With both HDMI ports disconnected, the reviewed `start-shell.sh` creates a headless output named `omarchy-pi` before launching Quickshell. If no output appears within its five-second wait, or the bar/IPC does not respond shortly afterward, capture the logs and stop. Do not create a headless output manually and then count this automatic-path test as passed. `omarchy-shell shell ping` confirms IPC readiness but does not prove visual rendering.

Probe the terminal only after the bar is responding. The previous Lua-configured Hyprland accepted `hyprctl eval 'hl.exec_cmd(...)'`; its plain `dispatch exec` returned a Lua parser error. Confirm exactly one mapped Foot client in the selected instance after invoking the reviewed minimal command. A failure is recorded as a failure of this session stage, not worked around by direct Foot launch.

```bash
hyprctl eval 'hl.exec_cmd("uwsm-app -- xdg-terminal-exec")'
for attempt in {1..30}; do
  if hyprctl -j clients | python3 -c 'import json, sys; sys.exit(not any(c.get("class", "").lower() == "foot" and c.get("mapped") for c in json.load(sys.stdin)))' >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done
hyprctl -j clients | tee "$PI_EVIDENCE/clients-after-foot.json"
python3 - "$PI_EVIDENCE/clients-after-foot.json" <<'PY'
import json, sys
clients = json.load(open(sys.argv[1], encoding="utf-8"))
foot = [client for client in clients if client.get("class", "").lower() == "foot" and client.get("mapped")]
assert len(foot) == 1, foot
PY
```

Choose the sole output name from `monitors.json` after checking that it is 1280×720 and, when HDMI is disconnected, headless. Run `grim` with the exact Wayland socket. The PNG header and dimensions are a mechanical check; a human must inspect the transferred image for the clock/workspace bar and Foot before claiming a rendered desktop.

```bash
PI_OUTPUT=$(python3 - "$PI_EVIDENCE/monitors.json" <<'PY'
import json, sys
monitors = json.load(open(sys.argv[1], encoding="utf-8"))
assert len(monitors) == 1, monitors
monitor = monitors[0]
assert (monitor["width"], monitor["height"]) == (1280, 720), monitor
assert monitor["name"] == "omarchy-pi", monitor
print(monitor["name"])
PY
)
grim -o "$PI_OUTPUT" "$PI_EVIDENCE/first-frame.png"
python3 - "$PI_EVIDENCE/first-frame.png" <<'PY'
import struct, sys
with open(sys.argv[1], "rb") as image:
    header = image.read(24)
assert header[:8] == b"\x89PNG\r\n\x1a\n"
assert struct.unpack(">II", header[16:24]) == (1280, 720)
PY
```

Keep the logs and frame private. A fresh SSH connection during the session must still report active `sshd`, `systemd-networkd`, and `wpa_supplicant@wld0`. Complete the observations promptly; run cleanup by minute four even if a probe is inconclusive. `RuntimeMaxSec=5min` limits the named transient unit but is not a substitute for explicit UWSM/PAM cleanup.

## 5. Stop, restore, and inspect preservation

Run `pi_session_cleanup` in the dedicated operator shell. It restores the saved VT first. If the initial `systemd-run` failed before the PAM session ID or compositor unit could be recorded, the trap still restores the VT and stops the named unit. From the second SSH connection, inspect any new local tty8 session and user compositor unit before targeted cleanup; do not guess a session ID. Never use `pkill -u sierra`, `loginctl terminate-user`, a user-manager restart, or a broad `uwsm stop` when another graphical session could exist.

```bash
pi_session_cleanup
trap - EXIT INT TERM HUP
sudo fgconsole
loginctl list-sessions
systemctl show -p LoadState -p ActiveState "$PI_UNIT"
systemctl --user list-units --all 'wayland-wm@*.service' 'graphical-session*.target' --no-pager
if systemctl --user show-environment | grep -Eq '^(WAYLAND_DISPLAY|XDG_CURRENT_DESKTOP|OMARCHY_PATH|OMARCHY_PI_MINIMAL_SESSION)='; then
  echo 'Graphical environment remains in the user manager; cleanup is incomplete' >&2
  exit 1
fi
systemctl --user show -p UnitFileState -p ActiveState fumon.service
systemctl --user list-unit-files --state=enabled --no-legend --no-pager | cmp - "$PI_EVIDENCE/user-enabled.before"
```

Require `fgconsole` to equal `PI_SAVED_VT`, no Sierra local tty8 PAM session, no active UWSM compositor or Quickshell/Foot process from this experiment, and no stale session variables in the user manager. If the cleanup function reports failure or any process survives, keep the second SSH connection and investigate the named unit/session identities before another stop. Do not clear the cleanup trap after a failed cleanup. The staged release and four config files remain for review; [the staged-session removal rules](../minimal-omarchy-session.md#removal) apply only after checking that each path is unchanged.

Run the post-package protected-state comparison again against the private baseline for this transaction. Set `PI_BASELINE` to that recorded directory; do not place its contents in this repository. Verify the expected filenames in the actual private record before using them. The pre-package account files legitimately gained the four reviewed Avahi rows, so a blanket `protected.sha256` check would fail even after a successful session. Check every other protected hash, require those four files to be byte-for-byte unchanged since this session's prestate, and confirm that the only difference from the backup's account files is one `avahi:` row per file. The unchanged-path count is derived from the actual baseline: it depends on which account files its protected-hash list contains. This is alongside the package-stage comparisons of installed versions/reasons, protected symlinks and absent paths, `/boot` inventory, LUKS metadata, EEPROM, firewall rules, routes, addresses, enabled units, default target, and Sierra groups. Review any unexpected difference before calling the run complete.

```bash
PI_BASELINE='/var/lib/omarchy-pi-deploy/REPLACE_WITH_RECORDED_BACKUP_ID'
sudo test -d "$PI_BASELINE"
for filename in protected.sha256 usb-key.sha256 luks.json eeprom system.tar; do
  sudo test -f "$PI_BASELINE/$filename"
done
sudo awk '$2 !~ /^\/etc\/(passwd|shadow|group|gshadow)$/' "$PI_BASELINE/protected.sha256" | sudo sha256sum --check --status
sudo sha256sum --check "$PI_EVIDENCE/account-files.before.sha256"
sudo python3 - "$PI_BASELINE/system.tar" <<'PY'
import pathlib, sys, tarfile
with tarfile.open(sys.argv[1], "r") as backup:
    for name in ("passwd", "shadow", "group", "gshadow"):
        old = backup.extractfile("etc/" + name).read()
        current = pathlib.Path("/etc/" + name).read_bytes().splitlines(keepends=True)
        avahi = [line for line in current if line.startswith(b"avahi:")]
        assert len(avahi) == 1, name
        assert b"".join(line for line in current if not line.startswith(b"avahi:")) == old, name
PY
sudo sha256sum --check "$PI_BASELINE/usb-key.sha256"
sudo cryptsetup luksDump --dump-json-metadata /dev/nvme0n1p2 | sudo cmp - "$PI_BASELINE/luks.json"
sudo rpi-eeprom-config | sudo cmp - "$PI_BASELINE/eeprom"
pacman -Dk
systemctl is-active sshd systemd-networkd wpa_supplicant@wld0
systemctl is-enabled sshd systemd-networkd wpa_supplicant@wld0
systemctl get-default
ip -br address show dev wld0
ip route
id sierra
```

Open a **new** independent SSH connection from the workstation after cleanup and rerun the mount and service checks. Existing SSH sockets alone do not establish recoverability. Once cleanup and preservation pass, transfer only the private frame/evidence from the Pi into a mode-0700 directory outside this synced project and inspect `first-frame.png` visually. Use the recorded `PI_RUN_ID`, not a wildcard.

```bash
# Workstation, after setting PI_RUN_ID to the value printed/recorded on the Pi:
umask 077
install -d -m 0700 "$HOME/.local/state/omarchy-pi-evidence/$PI_RUN_ID"
scp -F /dev/null -o StrictHostKeyChecking=yes sierra@192.168.178.21:/home/sierra/.local/state/omarchy-pi-session/$PI_RUN_ID/first-frame.png "$HOME/.local/state/omarchy-pi-evidence/$PI_RUN_ID/"
ssh -F /dev/null -o StrictHostKeyChecking=yes sierra@192.168.178.21 'findmnt -no SOURCE,FSTYPE,OPTIONS /; findmnt -no SOURCE,FSTYPE,OPTIONS /boot; systemctl is-active sshd systemd-networkd wpa_supplicant@wld0'
```

Record separately whether the local seat, UWSM environment, automatic headless output, Quickshell IPC, mapped Foot client, rendered frame, ordered cleanup, and protected-state checks passed. A valid PNG or IPC ping alone is not proof of a usable desktop. Defer portals, audio, idle/lock, network UI, and remote desktop to their own reviewed stages.

## Tested `start-hyprland` variant

The direct `/usr/bin/Hyprland` command above produced a visible watchdog warning. The separately reviewed wrapper variant removed that warning in the [captured result](session-result.md). Hyprland's [launch guidance](https://wiki.hypr.land/Getting-Started/Master-Tutorial/) recommends `start-hyprland`, and its [v0.56.2 source](https://github.com/hyprwm/Hyprland/blob/v0.56.2/start/src/core/Instance.cpp) forks the compositor as a child. On this Pi, `/usr/share/uwsm/plugins/start_hyprland.sh` is a packaged symlink to `hyprland.sh`. Require that plugin and a clean no-write preview before using the variant:

```bash
[[ -L /usr/share/uwsm/plugins/start_hyprland.sh ]]
[[ $(readlink /usr/share/uwsm/plugins/start_hyprland.sh) == hyprland.sh ]]
source "$HOME/.config/uwsm/env.d/90-omarchy-pi"
uwsm start -n -g -1 -U run -e -D Hyprland -- /usr/bin/start-hyprland -- --config /home/sierra/.config/hypr/hyprland.lua
```

The Pi's preview selected compositor ID `start-hyprland`, binary/plugin ID `start_hyprland`, and desktop name `Hyprland`. It named runtime drop-ins for `wayland-wm@start\x2dhyprland.service`; it did not write or start them. For a bounded live run, replace only the final executable and arguments of the `systemd-run` command above with `/usr/bin/start-hyprland -- --config /home/sierra/.config/hypr/hyprland.lua`. Keep the same tty8, PAM, environment, time limit, recovery connection and preservation gates. In addition, use an external 200-second timeout that sends `TERM` to the operator shell and gives its cleanup trap time to run.

The user unit's `MainPID` is the wrapper, while `hyprctl instances -j` reports its Hyprland child. Check **both** identities before probing or stopping the session:

```bash
PI_WM_UNIT="wayland-wm@$(systemd-escape start-hyprland).service"
PI_WRAPPER_PID=$(systemctl --user show "$PI_WM_UNIT" -p MainPID --value)
[[ $PI_WRAPPER_PID =~ ^[1-9][0-9]*$ ]]
[[ $(cat "/proc/$PI_WRAPPER_PID/comm") == start-hyprland ]]
mapfile -t PI_HYPR_CHILDREN < <(pgrep -P "$PI_WRAPPER_PID" -x Hyprland)
(( ${#PI_HYPR_CHILDREN[@]} == 1 ))
PI_HYPR_PID=${PI_HYPR_CHILDREN[0]}
[[ $(ps -o ppid= -p "$PI_HYPR_PID" | tr -d ' ') == "$PI_WRAPPER_PID" ]]
PI_WM_CGROUP=$(systemctl --user show "$PI_WM_UNIT" -p ControlGroup --value)
[[ -n $PI_WM_CGROUP ]]
[[ $(cut -d : -f 3 "/proc/$PI_WRAPPER_PID/cgroup") == "$PI_WM_CGROUP" ]]
[[ $(cut -d : -f 3 "/proc/$PI_HYPR_PID/cgroup") == "$PI_WM_CGROUP" ]]
```

Require a single `hyprctl instances -j` record whose PID equals `PI_HYPR_PID`; record its signature. Check the compositor's two named environment variables in `/proc/$PI_HYPR_PID/environ`, while using `PI_WRAPPER_PID` to identify the unit for targeted cleanup. Restore the saved VT first, then stop `PI_WM_UNIT` only if its live `MainPID` still equals `PI_WRAPPER_PID` and the recorded child remains directly parented to it or has exited. If an identity changed, inspect from the second SSH connection rather than guessing a process to kill. Then stop only `omarchy-pi-uwsm-smoke.service`, verify the recorded tty8 PAM session before terminating it if necessary, and perform every cleanup, fresh-SSH and protected-state check above. The successful wrapper run had no observed abort or remaining process after stop; it does not establish that all future wrapper exits will behave the same way.
