# First Hyprland and Foot smoke result — 23 September 2026

The first graphical smoke **passed compositor, renderer and client startup** on the encrypted Raspberry Pi 5. It did not capture a rendered frame, start the Omarchy Quattro session or test reboot. The transient test was stopped and the original console and access state were restored.

## Recovery and package transaction

- Private recovery run: `20260923T005446Z` under `/var/lib/omarchy-pi-deploy/`. Its encrypted off-Pi copy was decrypted and verified before installation. The backup contains sensitive boot, encryption and network material and must remain private.
- Fresh metadata in an isolated pacman database selected only the reviewed Expat update. All 107 archive names, versions, hashes, signatures and the local `pacman -Up` preview matched [the resolved manifest](packages-2026-09-23-resolved.tsv). The actual final prompt listed 107 packages, no removals or replacements, 707.54 MiB installed size and 706.99 MiB net change.
- One `pacman -U --asdeps` transaction installed 106 new packages and upgraded Expat to `2.8.5-1`. The four intended roots were marked explicit; Expat retained its original dependency install reason. The temporary udev sentinel suppressed the global device retrigger during the hook and was removed afterward. Pacman's post-transaction hooks created the `seat` group without adding Sierra to it.
- The resulting package set is exactly 297 packages: 24 explicit and 273 dependencies. The 191 old packages match their saved versions apart from the approved Expat update. `pacman -Dk` passed.

## Transient graphics test

The two files from commit `e5e68e31`, [hyprland.lua](session/hyprland.lua) and [foot.ini](session/foot.ini), were hash-checked and installed only under Sierra's new `~/.config/omarchy-pi-smoke/` directory. `Hyprland --verify-config` returned `config ok`; `fc-match` selected `/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Regular.ttf`.

The transient `omarchy-pi-smoke.service` ran `/usr/bin/Hyprland --config /home/sierra/.config/omarchy-pi-smoke/hyprland.lua` as UID 1000 with `PAMName=login` on tty8. Logind recorded a new active, non-remote Sierra session on `seat0`/VT 8. Its leader and the unit's main PID were both 7306. `hyprctl instances -j` showed one matching Hyprland 0.56.2 instance; `hyprctl configerrors` was empty.

Despite disconnected HDMI outputs and `AQ_NO_KMS_REQUIREMENT=1`, this packaged Hyprland/Aquamarine pair did **not** create the documented automatic `HEADLESS-0` output: `hyprctl -j monitors` initially returned `[]`. From the identified instance only, `hyprctl output create headless omarchy-smoke` created one 1280×720 at 60 Hz output with scale 1. The configured Foot start event had produced no running Foot process or client by that point. An instance-scoped `hyprctl eval 'hl.exec_cmd("foot --config=/home/sierra/.config/omarchy-pi-smoke/foot.ini")'` then launched Foot PID 7502. `hyprctl -j clients` showed it mapped and visible on that output as a native Wayland client. The cause of the missed initial Foot launch has not been isolated.

The compositor log identified `Renderer: V3D 7.1.7.0`, rather than llvmpipe. Aquamarine first reported an `EGL_BAD_MATCH` for GLES 3.2, then continued with its GLES 3.0 retry and hardware V3D renderer. This proves compositor and client startup on Pi graphics hardware; visual correctness still needs an actual frame capture or remote display check. Evidence files (`smoke-monitors.json`, `smoke-clients.json`, version, config errors, session properties, compositor log and journal) are saved privately in the recovery run directory.

One attempt to use `hyprctl dispatch exec` returned a Lua parser error and made no change. For this Lua-configured Hyprland version, the `hyprctl eval` form above succeeded.

## Cleanup and preservation

The test ran for about 140 seconds, below its five-minute unit limit. The named service and recorded PAM session were stopped, the saved VT 1 was restored, and no Hyprland or Foot process survived. The smoke unit was inactive and collected; `seat0` had no remaining session. No display manager or persistent unit was enabled.

After cleanup, a fresh independent SSH login succeeded. All 524 protected file hashes, 25 protected symlinks and the one previously absent protected path matched the backup. The USB key hash, LUKS metadata, EEPROM, boot ID and `/boot` path inventory matched. Root and boot mounts, enabled units, default target, firewall rules, routes, addresses and Sierra's group memberships remained unchanged. SSH, networkd and Wi-Fi remained active and enabled. `/etc` gained only 12 reviewed package-owned entries and 21 generated fontconfig symlinks pointing to fontconfig-owned files; no old `/etc` path was removed. `/etc/group` and `/etc/gshadow` gained only the expected `seat` entry.

## Next implementation boundary

The test leaves Hyprland, Mesa, Foot and their dependencies installed and the isolated smoke files in place. Full Omarchy Quattro startup remains untested: UWSM, Quickshell, portals, audio, startup helpers and remote viewing need their own bounded installation and session checks. A later headless startup should create its output explicitly before relying on the Foot or shell startup event. No reboot or unattended USB unlock test has occurred.
