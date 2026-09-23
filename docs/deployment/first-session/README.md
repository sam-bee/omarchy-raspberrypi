# Pi first-session deployment plan — 23 September 2026

**Current status: the first Hyprland/Foot smoke passed and was cleaned up.** The [deployment result](smoke-result.md) records the verified recovery backup, exact 107-archive package transaction, hardware V3D renderer, explicit headless output and mapped Foot client. Hyprland and Foot remain installed; the transient service and PAM session were stopped, VT 1 was restored, and post-test preservation checks passed. There has been no full Omarchy Quattro launch or reboot. The existing ARM planner remains plan-only. The steps below remain the reviewed recovery and reproduction procedure; they have already been applied once on this Pi.

## 1. Smallest useful change

Prove that the packaged Lua-capable Hyprland compositor and a Foot window can run on the existing encrypted Pi. This is a prerequisite smoke test, **not a complete Omarchy desktop**. Keep UWSM, Quickshell, portals, audio, Vulkan, RDP, display managers and automatic login/startup for subsequent reviewed steps. Full Quattro startup invokes deferred `udiskie` and power-profile helpers, while its portal configuration selects the deferred preview picker; copying its full session would obscure this first test.

| Explicit package root | Version in the reviewed snapshot | Purpose |
| --- | --- | --- |
| `extra/hyprland` | `0.56.2-3` | Compositor, Lua configuration and `hyprctl` |
| `extra/mesa` | `1:26.2.3-1` | Pi OpenGL userspace |
| `extra/foot` | `1.28.0-2` | Single test window |
| `extra/ttf-jetbrains-mono-nerd` | `3.5.1-2` | Explicit font matching the later Omarchy terminal |

The current exact transaction is [packages-2026-09-23-resolved.tsv](packages-2026-09-23-resolved.tsv): **106 new packages plus one reviewed Expat update, 145.05 MiB download**. Fresh native resolution selects `bubblewrap 0.13.0-1` and the sole pending installed-package update, `expat 2.8.4-1 → 2.8.5-1`. The four desktop roots are unchanged. The [original manifest](packages-2026-09-23.tsv) is retained as historical evidence; use the resolved manifest for deployment and [the installed baseline](installed-2026-09-23.txt) for recovery. It includes `hyprland-guiutils`, `lua`, `aquamarine`, `libinput`, `seatd` and other dependencies; the four roots are not the entire transaction. `seatd` is a dependency, not a request to enable its service or add Sierra to a group. Name/version/hash drift still requires review.

Exact persistent user payload, owned by `sierra:sierra`, files mode 0644, enclosing directory mode 0700:

- [session/hyprland.lua](session/hyprland.lua) → `/home/sierra/.config/omarchy-pi-smoke/hyprland.lua`.
- [session/foot.ini](session/foot.ini) → `/home/sierra/.config/omarchy-pi-smoke/foot.ini`.

Refuse deployment if the destination directory already exists or is a symlink; do not overwrite an earlier run. Use the explicit `--config` argument. Do not create `~/.config/hypr`, UWSM environment files, a Wayland `.desktop` entry, shell-profile changes or a persistent systemd unit. Package-owned session/unit files arrive with the packages; explicitly enabling/starting them is outside this plan, and their resulting state must be checked. Do not copy the Omarchy tree, `etc/` or `/etc/skel`, or run any native installer/provisioning/update command.

## 2. Verified preservation baseline

Read-only SSH and sudo worked on 23 September, with strict verification of the existing host key. Target: `sierra@192.168.178.21`, Raspberry Pi 5, UID/GID 1000, aarch64, kernel `6.18.52-1-rpi`.

| Preserve | Observed state; required result after each stage |
| --- | --- |
| Root and boot | `/` = ext4 `/dev/mapper/cryptroot`; `/boot` = vfat `/dev/nvme0n1p1` |
| Pi boot | EEPROM `BOOT_ORDER=0xf146`; `linux-rpi 6.18.52-1`; `raspberrypi-bootloader 20260915-1`; Pi overlay `vc4-kms-v3d-pi5`; initramfs `/boot/initramfs-linux.img` |
| Initramfs | `HOOKS=(base systemd autodetect microcode modconf keyboard sd-vconsole block sd-encrypt filesystems fsck)`; retain `sd-encrypt`, no `kms` |
| Encryption | LUKS2 UUID `f7900ae2-6634-4b98-8a08-a6a6902db87c`, keyslots 0 and 1; USB UUID `7695adc1-3681-459a-894f-80f1b615d430`, `/.cryptroot.key` present, root-owned mode 0400 |
| Network/SSH | `sshd`, `systemd-networkd`, `wpa_supplicant@wld0` active and enabled; DHCP on `wld0`; fresh independent SSH connection succeeds |
| Boot target/access | Already `graphical.target`, display manager absent; keep these states; no firewall, SSH, Wi-Fi, group-membership, or service-enable changes |
| Console | Foreground VT 1; tty8 getty inactive; both HDMI connectors disconnected; no local login session |

These observations do not retest automatic USB unlock at boot. There has been no reboot in this task.

## 3. Package gate, before installation

1. Retake the baseline and backups in [the command runbook](commands.md). Keep two independent SSH connections. Do not proceed if identity, mounts, boot settings, free space (at least 2 GiB on root), service state, or pre-existing package versions differ without review.
2. Resolve fresh metadata in a **separate copy** of the pacman database, retaining the Pi's actual `/etc/pacman.conf`, ALARM repositories and signature policy. This has passed for the recorded snapshot. Do not `pacman -Sy` into the live database or run an incidental `-Syu`. The full-upgrade preview must contain only the reviewed Expat update. Any additional installed-package update needs its own review before inclusion; do not silently omit it and create a partial upgrade.
3. Recreate the complete closure. Any different name, version, provider, replacement or removal requires a revised manifest and review. The only permitted pre-existing-package transition is `expat 2.8.4-1 → 2.8.5-1`, reviewed with a signed rollback archive. Reject other existing-package upgrades/downgrades/reinstalls, any protected package or path, and any kernel/initramfs/firmware, storage, network, SSH or firewall operation. In particular, no `mdadm`, Nautilus/gvfs/udisks2, generic kernel, bootloader, NetworkManager, SDDM, Plymouth or UFW.
4. Download the entire approved closure before installation. Verify SHA-256 and package signatures with the Pi's existing trusted keyring; retain archives and signatures for review. Do not weaken signature checks or use `--overwrite`, `--nodeps`, or hook suppression to force acceptance. Inspect `.PKGINFO`, `.INSTALL`, all archive paths, new hooks, service presets, tmpfiles/sysusers/udev rules, and both install **and removal** triggers against the actual installed hooks. Review the `pacman -Up` preview and final transaction prompt against the approved closure.

Archive review was completed for the resolved snapshot: the same 105 archives from the first review plus the reviewed new Bubblewrap and Expat archives. All 107 passed pacman's required/trusted signature and integrity checks using a copy of the Pi's keyring; their hashes matched fresh metadata and the installed transaction. The old signed Expat archive is retained for rollback. The missing `bubblewrap 0.12.0-1` was superseded by the verified `0.13.0-1`. See the [verification record](resolution.md) and [deployment result](smoke-result.md).

**Known hook issue:** `libinput` and `libwacom` install udev rules. The Pi's installed `35-systemd-udev-reload.hook` calls `systemd-hook udev-reload`, which otherwise runs `udevadm trigger -c change` for all devices, including networking. The proposed transaction explicitly uses that helper's supported temporary sentinel, `/etc/systemd/do-not-udevadm-trigger-on-update`, to allow rule reload while suppressing the global device trigger. Preserve any pre-existing sentinel; remove only one created by this deployment. Use the same guard on removal. This narrowly scoped temporary file is the only planned administrator-written `/etc` change. Do not disable the hook itself. Recheck the helper implementation before using the guard.

The reviewed archives introduce ordinary font/image/MIME caches, system/user unit files and a seatd sysusers rule. [The archive review](archive-review.md) lists these effects, including creation of group `seat`. Installing a unit is not permission to start or enable it. Existing system hooks can still reload systemd/user managers or create declared users/groups; package removal does not undo every generated cache or sysusers effect. Neither the prior 105 archives nor the added Bubblewrap/Expat archives match the listed mkinitcpio-sensitive paths. Recheck the installed hooks before applying the frozen transaction.

## 4. Apply order, only after the package gate passes

1. Create and verify the private recovery bundle, including an encrypted off-Pi copy; retain exact package inventories and service states. Do not put secrets or recovery archives in this Git repository or the synced workspace.
2. Install only the reviewed 107 local archives in one transaction under the temporary udev guard. Mark the four roots explicit and new dependencies as dependencies; preserve Expat's original install reason. Record the actual package difference even if the transaction fails. Remove the temporary guard if this run created it.
3. Run preservation checks immediately: hashes, LUKS metadata, EEPROM, original package versions with only the reviewed Expat exception, service enablement/activity, firewall rules, routes and a **new** SSH connection. Any unexpected difference is a stop condition; do not reboot to troubleshoot it.
4. Copy only the two session files. Validate with `Hyprland --verify-config --config /home/sierra/.config/omarchy-pi-smoke/hyprland.lua`, inspect exit status/output, and check `fc-match 'JetBrainsMono Nerd Font'`. A parse failure stops here.
5. Run the separately bounded console/session experiment below. Stop it afterward, restore the original foreground VT, and repeat preservation checks.

## 5. SSH-driven session experiment

An ordinary SSH shell has no active logind graphics seat. The smoke used a **transient** system service with `User=sierra`, `PAMName=login`, controlling tty8 and a five-minute lifetime, after confirming that tty8 and seat0 were unused. The existing login PAM stack includes `pam_systemd`; the concrete command is in the runbook. This produced the required local Sierra session and unprivileged compositor without enabling a display manager, changing device permissions, adding Sierra to a group or enabling lingering.

The documented `AQ_NO_KMS_REQUIREMENT=1` was scoped to this one service because HDMI was disconnected. It is not a replacement for an active logind seat. Contrary to the documented automatic `HEADLESS-0` behavior, the packaged pair started with no monitor. Creating a headless output through the identified instance worked; the exact command and evidence are in the [result](smoke-result.md). This does not authorize boot-overlay, udev, PAM or group changes.

Pass conditions: a new non-remote `sierra` session on `seat0`/tty8 is active; the compositor runs as UID 1000; its chosen `hyprctl` instance reports no config errors, one headless output at the requested size, and a Foot client; logs identify the Pi renderer rather than software fallback. Capture the journal and `hyprctl -j monitors`, `clients`, `version`, and `configerrors`. Without an actual captured/rendered frame, report only compositor/client startup, not visual correctness. There is no remote desktop server in this step.

After checks, stop the named service and any recorded PAM session belonging to this experiment, restore the saved VT, and confirm no test compositor/client remains. Do not kill the whole user manager or all Sierra processes; SSH uses that same account.

## 6. Rollback and acceptance

The runbook stops only this experiment, restores the VT, removes the two namespaced files, removes exactly the newly installed approved package set with `pacman -R` (no recursive/orphan deletion), and restores the signed old Expat package and its original install reason. If other packages were installed meanwhile, the automatic rollback stops for review. Never restore a saved pacman database over installed files or blindly unpack all of `/etc` over a live system.

Protected files should never change. If they do, preserve the evidence and restore only the identified files from the recovery archive while the current SSH session still works; do not rebuild initramfs as a shortcut. Restoring LUKS headers/keyslots or EEPROM is outside normal rollback and requires a separately reviewed recovery action. USB rescue media remains untouched.

If SSH disappears, locate the Pi on the authorized home LAN and retry with its verified host key. If network/SSH or boot is broken, remote commands cannot guarantee recovery: use HDMI/keyboard and the recorded passphrase, or the existing rescue USB, then mount the correct encrypted NVMe by UUID and restore the identified files. Do not format any disk or rewrite the rescue USB. Keep the encrypted off-Pi backup and its decryption passphrase available for that case.

Only a later controlled `sudo systemctl reboot`, with Sierra available for fallback and the unlock USB inserted, can test the entire unattended boot path. After reconnecting, verify a changed boot ID, encrypted root, boot mount, Wi-Fi/SSH, EEPROM/boot hashes and unchanged package versions. A successful graphical smoke test or hash comparison alone is not that evidence. Do not schedule an automatic reboot in this plan.

## Review record and references

Luna 6 xhigh independently checked session scope, recovery risks, the replacement Bubblewrap archive and the narrow Expat update; the parent checked live state, native resolution, trusted package verification and actual Bubblewrap sandbox operation. The review narrowed the initial transaction, isolated user configuration, rejected an assumed SSH graphics seat, and added a specific udev safeguard. The [first-session result](smoke-result.md) now documents the completed graphical smoke and preservation checks. The full Omarchy desktop and unattended reboot remain untested.

- Repository context: [ARM audit](../../arm64-package-audit.md), [compatibility boundary](../../arm64.md), `default/hypr/autostart.lua`, `config/hypr/xdph.conf`, and `default/wayland-sessions/omarchy.desktop`.
- [Hyprland 0.56.2 CLI source](https://github.com/hyprwm/Hyprland/blob/v0.56.2/src/main.cpp) confirms the explicit config and verification options.
- [Hyprland virtual GPU documentation](https://wiki.hypr.land/configuring/extra/virtual-gpu/) describes the no-output flag and headless output; [systemd execution documentation](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html) describes PAM and controlling terminals. Neither source establishes that this exact Pi launch has passed.
