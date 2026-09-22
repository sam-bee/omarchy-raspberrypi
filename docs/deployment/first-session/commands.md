# Proposed commands — do not execute before the plan's gates pass

These are ordered operator steps, not one unattended script. Commands run in an **interactive SSH terminal as Sierra** unless labeled workstation. Preserve the second SSH connection throughout. Review the result of each stage before continuing. All `$PI_DEPLOY_*` variables are local shell variables, not system settings.

## Backup and prestate (first permitted future Pi writes)

```bash
set -euo pipefail
umask 077
export PI_DEPLOY_RUN="$(date -u +%Y%m%dT%H%M%SZ)"
export PI_DEPLOY_BACKUP="/var/lib/omarchy-pi-deploy/$PI_DEPLOY_RUN"
sudo install -d -m 0700 "$PI_DEPLOY_BACKUP"
sudo bash -s -- "$PI_DEPLOY_BACKUP" <<'ROOT'
set -euo pipefail
B=$1
umask 077
test ! -e /var/lib/pacman/db.lck
pacman -Q > "$B/before.packages"
pacman -Qqe > "$B/before.explicit"
pacman -Qqd > "$B/before.dependencies"
systemctl get-default > "$B/default-target"
systemctl list-unit-files --state=enabled --no-legend --no-pager > "$B/enabled-units"
systemctl is-active sshd systemd-networkd wpa_supplicant@wld0 > "$B/services-active"
systemctl is-enabled sshd systemd-networkd wpa_supplicant@wld0 > "$B/services-enabled"
ip -br address > "$B/addresses"
ip route > "$B/routes"
cat /proc/sys/kernel/random/boot_id > "$B/boot-id"
fgconsole > "$B/foreground-vt"
rpi-eeprom-config > "$B/eeprom"
cryptsetup luksDump --dump-json-metadata /dev/nvme0n1p2 > "$B/luks.json"
cryptsetup luksHeaderBackup /dev/nvme0n1p2 --header-backup-file "$B/luks-header.bin"
sfdisk --dump /dev/nvme0n1 > "$B/nvme-partitions"
sfdisk --dump /dev/sda > "$B/usb-partitions"
nft list ruleset > "$B/nft.rules"
iptables-save > "$B/iptables.rules"
ip6tables-save > "$B/ip6tables.rules"
protected=()
for p in /boot /etc/fstab /etc/crypttab /etc/crypttab.initramfs \
  /etc/mkinitcpio.conf /etc/mkinitcpio.conf.d /etc/mkinitcpio.d \
  /etc/pacman.conf /etc/pacman.d /etc/systemd /etc/ssh /etc/wpa_supplicant \
  /etc/resolv.conf /etc/hosts /etc/hostname /etc/nftables.conf /etc/iptables \
  /etc/sudoers /etc/sudoers.d /etc/pam.d /etc/security /etc/modprobe.d \
  /etc/modules-load.d /etc/udev /etc/passwd /etc/shadow; do
  if [[ -e $p || -L $p ]]; then
    protected+=("$p")
  else
    printf '%s\n' "$p" >> "$B/protected-absent.paths"
  fi
done
printf '%s\n' "${protected[@]}" > "$B/protected-roots.paths"
find "${protected[@]}" -xdev -type f -exec sha256sum {} + > "$B/protected.sha256"
find "${protected[@]}" -xdev -type l -printf '%p\t%l\n' > "$B/protected-symlinks.tsv"
find /boot /etc -xdev -printf '%y\t%p\n' > "$B/before-config.paths"
id sierra > "$B/sierra-groups"
sha256sum /run/systemd/cryptsetup/keydev-cryptroot/.cryptroot.key > "$B/usb-key.sha256"
tar --acls --xattrs --numeric-owner -C / -cpf "$B/system.tar" \
  etc boot var/lib/pacman/local var/lib/pacman/sync var/log/pacman.log \
  run/systemd/cryptsetup/keydev-cryptroot/.cryptroot.key
sha256sum "$B/system.tar" "$B/luks-header.bin" > "$B/backup.sha256"
sha256sum --check "$B/backup.sha256"
tar -tf "$B/system.tar" > "$B/backup-members"
ROOT
```

Confirm `/dev/sda2` still has the recorded USB UUID before the partition-table read above. Backups are sensitive: the archive includes Wi-Fi configuration, SSH keys, the unlock key and a LUKS header. Do not print/upload their contents. No LUKS or USB content is changed by these backup commands. Record the run ID in the operator's private notes.

Create an encrypted transport bundle on the Pi; choose and retain a separate recovery passphrase using GPG's interactive prompt:

```bash
sudo tar -C "$PI_DEPLOY_BACKUP" -cf - . | \
  gpg --symmetric --cipher-algo AES256 \
    --output "$HOME/omarchy-pi-recovery-$PI_DEPLOY_RUN.tar.gpg"
```

On the workstation, set `PI_DEPLOY_RUN` to the recorded value, copy the encrypted bundle outside this synced project, and verify it can be decrypted/listed. Do not continue if this fails:

```bash
umask 077
mkdir -p "$HOME/.local/state/omarchy-pi-recovery"
chmod 0700 "$HOME/.local/state/omarchy-pi-recovery"
scp -F /dev/null -o StrictHostKeyChecking=yes \
  "sierra@192.168.178.21:omarchy-pi-recovery-$PI_DEPLOY_RUN.tar.gpg" \
  "$HOME/.local/state/omarchy-pi-recovery/"
set -o pipefail
gpg --decrypt "$HOME/.local/state/omarchy-pi-recovery/omarchy-pi-recovery-$PI_DEPLOY_RUN.tar.gpg" | tar -tf - >/dev/null
```

## Isolated package resolution and review

Back on the Pi; these commands update only the private resolution database, not the live sync database:

```bash
sudo install -d -m 0700 "$PI_DEPLOY_BACKUP/resolve" "$PI_DEPLOY_BACKUP/packages"
sudo cp -a /var/lib/pacman/local "$PI_DEPLOY_BACKUP/resolve/"
sudo cp -a /var/lib/pacman/sync "$PI_DEPLOY_BACKUP/resolve/"
sudo pacman --dbpath "$PI_DEPLOY_BACKUP/resolve" -Sy
sudo pacman --dbpath "$PI_DEPLOY_BACKUP/resolve" -Sup --print-format '%n %v %r'
sudo pacman --dbpath "$PI_DEPLOY_BACKUP/resolve" -Sp --needed \
  --print-format '%n %v %r %a %s %l' \
  extra/hyprland extra/mesa extra/foot extra/ttf-jetbrains-mono-nerd
```

**Stop here for the package gate.** The full-upgrade preview must be empty and the install closure must receive the complete review specified in the plan. Recheck the real installed database has not changed since copying it. If refreshed metadata differs from the dated manifest, save a replacement manifest and review it before download/install; never assume the old 106-package count still applies. This private database is disposable and is never copied back over the live database.

After a fresh manifest is approved, download against that exact resolution:

```bash
sudo pacman --dbpath "$PI_DEPLOY_BACKUP/resolve" \
  --cachedir "$PI_DEPLOY_BACKUP/packages" -Sw --needed \
  extra/hyprland extra/mesa extra/foot extra/ttf-jetbrains-mono-nerd
```

Before continuing, require matching detached `.sig` files for each approved archive (fetch the archive URL plus `.sig` if the downloader did not retain it), successful `sudo pacman-key --verify ARCHIVE.sig ARCHIVE`, expected SHA-256, exact metadata/file/hook review, and no extra archives in the private package directory. `ARCHIVE` here denotes each reviewed file, not a wildcard transaction. Save approved package names, one per line sorted under `LC_ALL=C`, as root-owned `$PI_DEPLOY_BACKUP/approved-new.names`. These must all be absent from the prestate. Keep the reviewed manifest and archive analysis with the private run record.

## Install with the temporary udev guard

First collect exactly the reviewed archives (no signatures/partial downloads), check `pacman -Up`, and compare its names/versions with the approved manifest. The package directory is root-owned; it must contain only the approved archive files and their signatures:

```bash
mapfile -t PI_DEPLOY_ARCHIVES < <(sudo find "$PI_DEPLOY_BACKUP/packages" \
  -maxdepth 1 -type f -name '*.pkg.tar.*' ! -name '*.sig' ! -name '*.part' | LC_ALL=C sort)
test "${#PI_DEPLOY_ARCHIVES[@]}" -gt 0
sudo pacman -Up --print-format '%n %v' -- "${PI_DEPLOY_ARCHIVES[@]}"
```

Only after that preview is accepted, run the transaction. Keep other package maintenance stopped throughout install/removal; the lock check is a preflight, not a reservation against another administrator starting pacman. Answer its final prompt only if it contains exactly the approved additions and no other changes. The trap restores sentinel absence on normal completion/failure; if the shell is killed, inspect and restore it manually before finishing. A pre-existing sentinel must stay untouched.

```bash
sudo bash -s -- "$PI_DEPLOY_BACKUP" "${PI_DEPLOY_ARCHIVES[@]}" <<'ROOT'
set -euo pipefail
B=$1
shift
test ! -e "$B/udev-guard-created"
test ! -e /var/lib/pacman/db.lck
guard=/etc/systemd/do-not-udevadm-trigger-on-update
created=0
cleanup() { if (( created )); then rm -- "$guard"; rm -- "$B/udev-guard-created"; fi; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
if [[ ! -e $guard && ! -L $guard ]]; then
  touch "$B/udev-guard-created"
  install -m 0644 /dev/null "$guard"
  created=1
fi
pacman -U --asdeps -- "$@" </dev/tty
pacman -D --asexplicit hyprland mesa foot ttf-jetbrains-mono-nerd
ROOT
```

Capture `pacman -Q` after the attempt even if it failed; apply the preservation checks below before staging configuration. Never retry a failed package transaction blindly. After an interrupted connection/process, check the private `udev-guard-created` marker. Once no transaction is running, remove the sentinel only if this marker proves this run created it and it is still the expected root-owned empty regular file; then remove the marker. If it was pre-existing or changed unexpectedly, stop for inspection. Never remove pacman's lock while a transaction is still running. Apply this recovery rule to removal too.

## Stage and parse the two session files

Copy only `session/hyprland.lua` and `session/foot.ini` from the reviewed repository commit to a temporary Sierra-owned staging directory. Verify their SHA-256 against the workstation copy. With that directory named `$PI_DEPLOY_STAGE`:

```bash
test "$HOME" = /home/sierra
test ! -L /home
test ! -L /home/sierra
test "$(stat -c %u /home/sierra)" = 1000
test ! -L "$HOME/.config"
if [[ -e $HOME/.config ]]; then
  test -d "$HOME/.config"
  test "$(stat -c %u "$HOME/.config")" = 1000
fi
test ! -e "$HOME/.config/omarchy-pi-smoke"
test ! -L "$HOME/.config/omarchy-pi-smoke"
install -d -m 0700 "$HOME/.config/omarchy-pi-smoke"
install -m 0644 "$PI_DEPLOY_STAGE/hyprland.lua" "$HOME/.config/omarchy-pi-smoke/hyprland.lua"
install -m 0644 "$PI_DEPLOY_STAGE/foot.ini" "$HOME/.config/omarchy-pi-smoke/foot.ini"
Hyprland --verify-config --config "$HOME/.config/omarchy-pi-smoke/hyprland.lua"
fc-match 'JetBrainsMono Nerd Font'
```

Require `fc-match` to report the intended JetBrains Mono Nerd Font family and file, not a fallback font. Clean up only this run's staging files after their hashes match.

## Transient session: a separate five-minute experiment

Check `loginctl list-sessions`, `loginctl seat-status seat0`, `systemctl show -p LoadState -p ActiveState -p SubState getty@tty8.service omarchy-pi-smoke.service`, and `sudo fgconsole` first. Inspect the properties instead of treating an expected inactive/not-found `systemctl status` exit as a fatal error. Save the foreground VT immediately before switching it. Require no active seat0 user, an unused tty8, no existing smoke unit/process and a working second SSH session. Never replace or stop someone else's session.

Install cleanup before switching the VT. Run this experiment in its own interactive Bash shell so these traps do not replace unrelated operator traps. VT restoration is attempted first even if the unit/session has already vanished. A surviving PAM session is terminated only after checking its identity; failed cleanup requires investigation from the second SSH session.

```bash
sudo fgconsole | sudo tee "$PI_DEPLOY_BACKUP/experiment-vt" >/dev/null
PI_DEPLOY_VT=$(sudo cat "$PI_DEPLOY_BACKUP/experiment-vt")
PI_DEPLOY_SESSION_ID=
pi_smoke_cleanup() {
  local failed=0 load_state
  sudo chvt "$PI_DEPLOY_VT" || failed=1
  if load_state=$(systemctl show -p LoadState --value omarchy-pi-smoke.service); then
    if [[ $load_state != not-found ]]; then
      sudo systemctl stop omarchy-pi-smoke.service || failed=1
    fi
  else
    failed=1
  fi
  if [[ -n $PI_DEPLOY_SESSION_ID ]] && loginctl show-session "$PI_DEPLOY_SESSION_ID" >/dev/null 2>&1; then
    if [[ $(loginctl show-session "$PI_DEPLOY_SESSION_ID" -p Name --value) == sierra &&
          $(loginctl show-session "$PI_DEPLOY_SESSION_ID" -p Seat --value) == seat0 &&
          $(loginctl show-session "$PI_DEPLOY_SESSION_ID" -p VTNr --value) == 8 ]]; then
      sudo loginctl terminate-session "$PI_DEPLOY_SESSION_ID" || failed=1
    else
      failed=1
    fi
  fi
  return "$failed"
}
trap pi_smoke_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
sudo chvt 8
sudo systemd-run --unit=omarchy-pi-smoke --collect --service-type=exec \
  --property=User=sierra --property=PAMName=login \
  --property=WorkingDirectory=/home/sierra \
  --property=TTYPath=/dev/tty8 --property=StandardInput=tty \
  --property=StandardOutput=journal --property=StandardError=journal \
  --property=TTYReset=yes --property=TTYVHangup=yes \
  --property=Restart=no --property=RuntimeMaxSec=5min \
  --property=TimeoutStopSec=15s --property=KillMode=control-group \
  --setenv=HOME=/home/sierra --setenv=XDG_RUNTIME_DIR=/run/user/1000 \
  --setenv=XDG_SESSION_TYPE=wayland --setenv=XDG_SESSION_DESKTOP=Hyprland \
  --setenv=XDG_CURRENT_DESKTOP=Hyprland --setenv=AQ_NO_KMS_REQUIREMENT=1 \
  /usr/bin/Hyprland --config /home/sierra/.config/omarchy-pi-smoke/hyprland.lua
```

If `systemd-run` fails, immediately restore the saved VT with the stop commands below. Do not leave tty8 as the foreground VT. In all cases verify the PAM session: record its ID from `loginctl list-sessions`, then `loginctl show-session ID -p Name -p Remote -p Seat -p VTNr -p Active -p Leader`. Stop if it is not Sierra's active local tty8 session. PAM can move the process into a session scope, so the transient service's cgroup limit alone is not a proven cleanup mechanism; monitor the five-minute limit and use explicit session cleanup too.

Run `hyprctl instances -j`, identify this experiment's instance by its PID/config/session, and set `HYPRLAND_INSTANCE_SIGNATURE` to that instance only. Then run:

```bash
hyprctl version
hyprctl configerrors
hyprctl -j monitors
hyprctl -j clients
sudo journalctl -u omarchy-pi-smoke.service --no-pager
```

Set `PI_DEPLOY_SESSION_ID` to the **recorded new tty8 session** during the session check, never an SSH/manager session. Stop whether successful or not:

```bash
pi_smoke_cleanup
```

With `--collect` the unit may already be gone; that must not prevent session cleanup or VT restoration. Confirm the recorded compositor/Foot processes exited, the experiment's PAM session ended, and `sudo fgconsole` matches the saved VT; never use `pkill -u sierra`, `loginctl terminate-user`, or a user-manager restart. If startup failed before the PAM session ID was recorded, inspect new local tty8 sessions before declaring cleanup complete. After a lost shell, restore the saved VT from the second SSH session first, then identify and stop only this experiment's unit/session.

Only after those checks pass, clear the experiment shell's traps with `trap - EXIT INT TERM HUP`.

## Preservation checks, after every stage and after rollback

Run `sudo sha256sum --check "$PI_DEPLOY_BACKUP/protected.sha256"` and the USB-key hash check privately; all pre-existing **protected** files must match. Compare every recorded symlink target and the absence of `protected-absent.paths`, LUKS JSON (`cryptsetup luksDump --dump-json-metadata`), `rpi-eeprom-config`, enabled-unit state, default target, firewall rule content, and the versions of **every** pre-existing package with the backup. Compare firewall rules ignoring the generated `iptables-save` timestamp comments; do not mistake elapsed counters or DHCP lifetimes for configuration changes. Inventory additions against `before-config.paths`: none in `/boot`, only reviewed package paths in `/etc`; a hash check of old files does not detect additions. The full `/etc` backup allows review of ordinary generated state outside the protected list: linker caches and font configuration may change, and `group`/`gshadow` plus their backup files may gain only the reviewed `seat` group. No existing account or membership may change. Inspect these differences privately rather than publishing credential files.

```bash
pacman -Dk
findmnt -no SOURCE,FSTYPE,OPTIONS /
findmnt -no SOURCE,FSTYPE,OPTIONS /boot
sudo cryptsetup status cryptroot
systemctl is-active sshd systemd-networkd wpa_supplicant@wld0
systemctl is-enabled sshd systemd-networkd wpa_supplicant@wld0
ip -br address show dev wld0
ip route
```

Also open a fresh, independent `ssh -F /dev/null -o StrictHostKeyChecking=yes sierra@192.168.178.21` from the workstation and run `id` plus the mount/service checks. Passing on an already established SSH connection is insufficient. Preserve journals and `/var/log/pacman.log` if any check fails. Only expected new package files/caches and this run's recorded artifacts are allowed.

Specifically inspect `systemctl show -p UnitFileState -p ActiveState seatd.service fancontrol.service healthd.service lm_sensors.service sensord.service` and `systemctl --user show -p UnitFileState -p ActiveState foot-server.service foot-server.socket`. None may acquire an enabled or active state through this transaction. Use property inspection rather than unguarded `is-enabled`/`status` calls for these deliberately inactive units. Verify Sierra's supplementary groups remain unchanged.

## Routine rollback

Stop the experiment/restore the VT as above. Because the destination was required to be absent before staging, remove only these files and the empty directory (if the user has since edited them, save that work first):

```bash
rm -- "$HOME/.config/omarchy-pi-smoke/hyprland.lua" "$HOME/.config/omarchy-pi-smoke/foot.ini"
rmdir -- "$HOME/.config/omarchy-pi-smoke"
```

For package rollback, calculate the actual additions, including after an interrupted/failed transaction. Refuse unexpected additions, missing pre-existing packages or changes to any pre-existing version. No recursive dependency/orphan purge:

```bash
sudo bash -s -- "$PI_DEPLOY_BACKUP" <<'ROOT'
set -euo pipefail
B=$1
export LC_ALL=C
test ! -e "$B/udev-guard-created"
pacman -Q | sort > "$B/rollback.packages"
join <(sort "$B/before.packages") "$B/rollback.packages" | \
  awk '$2 != $3 {bad=1} END {exit bad}'
comm -23 <(awk '{print $1}' "$B/before.packages" | sort) \
  <(awk '{print $1}' "$B/rollback.packages" | sort) > "$B/missing-old.names"
test ! -s "$B/missing-old.names"
comm -13 <(awk '{print $1}' "$B/before.packages" | sort) \
  <(awk '{print $1}' "$B/rollback.packages" | sort) > "$B/actual-new.names"
comm -23 "$B/actual-new.names" "$B/approved-new.names" > "$B/unexpected-new.names"
test ! -s "$B/unexpected-new.names"
mapfile -t added < "$B/actual-new.names"
test ! -e /var/lib/pacman/db.lck
guard=/etc/systemd/do-not-udevadm-trigger-on-update
created=0
cleanup() { if (( created )); then rm -- "$guard"; rm -- "$B/udev-guard-created"; fi; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
if [[ ! -e $guard && ! -L $guard ]]; then
  touch "$B/udev-guard-created"
  install -m 0644 /dev/null "$guard"
  created=1
fi
if (( ${#added[@]} )); then
  pacman -R -- "${added[@]}" </dev/tty
fi
pacman -Dk
ROOT
```

Review the removal prompt and permit only the recorded additions. Let dependency checks stop the removal if anything now needs them. Compare package versions and explicit/dependency reasons with prestate afterward. Retain backups/logs; do not delete pre-existing users/groups or caches indiscriminately. Inspect any newly generated config, cache, sysusers group or `.pacsave` against the archive review before cleaning it up. This restores the package set, not a byte-for-byte filesystem image.

If a protected file changed unexpectedly, recover only the identified file after reviewing the difference. For example, **only if that exact file needs restoration**, while `/boot` is still the verified NVMe boot partition:

```bash
sudo tar --numeric-owner --same-owner -xpf "$PI_DEPLOY_BACKUP/system.tar" -C / -- boot/config.txt
sudo sha256sum --check "$PI_DEPLOY_BACKUP/protected.sha256"
```

For ext4 configuration files include `--acls --xattrs` when extracting their exact archive member. Restore symlinks/permissions as recorded. Do not unpack all of `system.tar`, restore `/var/lib/pacman`, run `mkinitcpio`, restore a LUKS header, or alter EEPROM as routine rollback. Recovery from lost SSH or boot requires the separate physical/rescue path described in the plan; no remote timer can repair an inaccessible encrypted root.
