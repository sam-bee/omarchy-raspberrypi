# Raspberry Pi 5 package audit — 22 September 2026

This is a read-only inventory of the existing Arch Linux ARM Pi, not an install rehearsal or approval to apply the [ARM64 package policy](arm64-packages.md). No package database refresh, download, installation, service change, configuration write, or reboot was performed on the Pi.

## Method and freshness

Over password-authenticated SSH with strict checking of the existing host key, we read `pacman-conf --repo-list`, `pacman -Q`, `pacman -Qm`, `pacman -Dk`, `pacman -Si`, `pacman -Sp --noconfirm --print-format`, `pacman -Sup --noconfirm --print-format`, and the installed libalpm hook files. `-Sp` and `-Sup` printed resolution from the Pi's **existing** sync databases; neither option included `-y` or applied a transaction.

The configured repositories are `core`, `extra`, `alarm`, and `aur`. At audit time, the local `core` and `extra` sync databases were dated 21 September, `alarm` 19 September, and `aur` 21 August 2026. These results must be checked again before a later transaction because repo versions and dependency closure can change. `pacman -Dk` reported no database errors, `pacman -Qm` found no foreign packages, and the cached full-upgrade preview printed no pending upgrades.

The Pi still had `linux-rpi 6.18.52-1`, `mkinitcpio 42-1`, `raspberrypi-bootloader 20260915-1`, `openssh 10.5p1-1`, `systemd 261.3-1`, and `wpa_supplicant 2:2.12-1`. Hyprland, Quickshell, UWSM, both portal backends, Mesa, and PipeWire were not installed. `python`, `git`, `libsecret`, and `polkit` were already installed.

## Package sources and versions

The original policy had 58 first-transaction package targets (candidates and replacements). The Pi's cached sync databases resolved 57 by exact name: 54 in `extra`, three in `core`, and none in `alarm` or `aur`. The sole missing exact name was `vi`; the Pi already has `/usr/bin/vi` from `ex-vi-compat 3-1`, which the `extra` repository provides and advertises as a replacement for `vi`. The policy now names that provider directly.

| Component | Pi sync package/version | Audit result |
| --- | --- | --- |
| Hyprland | `extra/hyprland 0.56.2-3` | Depends on `lua`, `mesa`, and `hyprland-guiutils`; Lua configuration must still be exercised. |
| Quickshell | `extra/quickshell 0.3.1-1` | Depends on Qt 6, `libpipewire`, and `polkit`; shipped QML imports still need a runtime check. |
| Session and terminal | `extra/uwsm 0.27.0-1`, `extra/xdg-terminal-exec 0.14.3-2`, `extra/foot 1.28.0-2` | Names resolve; the `uwsm` session and terminal bindings are untested. |
| Portals | `extra/xdg-desktop-portal-hyprland 1.4.1-2`, `extra/xdg-desktop-portal-gtk 1.15.3-1` | Both resolve; activation and screen-share fallback are untested. |
| Audio | `extra/pipewire 1:1.6.8-1`, `extra/pipewire-pulse 1:1.6.8-1`, `extra/wireplumber 0.5.17-1` | Packages resolve; `pipewire-pulse` depends on `avahi` and conflicts with `pulseaudio`. Neither `avahi` nor `pulseaudio` was installed. |
| Pi graphics | `extra/mesa 1:26.2.3-1`, `extra/vulkan-broadcom 1:26.2.3-1` | Both resolve; neither was installed or runtime-tested. Firmware and the existing overlay remain untouched. |
| Font replacement | `extra/ttf-jetbrains-mono-nerd 3.5.1-2` | Resolves; the configured `JetBrainsMono Nerd Font` family needs verification after installation. |

The upstream base list's `lua51` and `luarocks` can remain deferred: the available Hyprland package depends on `lua`, and the shipped Lua configuration runs inside Hyprland. An external `lua` command is only an optional helper for the keybinding menu. This does **not** prove the packaged Hyprland build parses our configuration correctly.

## Transaction and hook findings

With the 57 exact names that originally resolved, `pacman -Sp` printed 414 package entries, of which 410 were absent from the installed package list. This is a large first transaction, even though the cached full-upgrade preview was empty. The preview does not show all file-triggered hooks or package install scripts.

One original candidate crossed the protected boot boundary: `nautilus` pulls `gvfs`, then `udisks2`, `libblockdev-mdraid`, and `mdadm` in this repository snapshot. `mdadm` was not installed. The Pi's `/usr/share/libalpm/hooks/90-mkinitcpio-install.hook` matches `usr/bin/mdadm` on install and runs `/usr/share/libalpm/scripts/mkinitcpio install` after the transaction. Thus adding the proposed file manager would invoke an initramfs rebuild path. The current Pi initramfs must remain under explicit review. The dependency chain is also documented by [Arch Linux ARM's udisks2](https://archlinuxarm.org/packages/aarch64/udisks2) and [libblockdev-mdraid](https://archlinuxarm.org/packages/aarch64/libblockdev-mdraid) metadata.

A second read-only preview omitted Nautilus and `egl-wayland`, and named the already-installed `ex-vi-compat` provider. It printed 342 package entries, 337 absent locally. `mdadm`, `udisks2`, `gvfs`, `linux-rpi`, `mkinitcpio`, `systemd`, `openssh`, `networkmanager`, `plymouth`, `sddm`, and `ufw` were absent from that printed list. This removes the **identified** `mdadm` hook trigger; it is not a complete proof about package file lists, scriptlets, or other hook triggers. Standard installed hooks for system users, tmpfiles, systemd reloads, and other paths can still run when their file triggers match.

`pipewire-pulse` pulls `avahi` as a hard dependency in this snapshot, despite the original policy deferring Avahi. Its policy row is now a package candidate with a requirement to review effects and keep service ownership unchanged. `egl-wayland` is available on aarch64, but its [package description](https://archlinuxarm.org/packages/aarch64/egl-wayland) is an NVIDIA EGLStream platform, not a generic Pi V3D Wayland requirement; it is excluded from the first transaction.

## Runtime gaps still to resolve

- The repository's Hyprland Lua configuration targets Quattro's newer Lua-capable compositor; version availability alone does not validate parsing, bindings, or the Pi graphics path.
- Quickshell imports Hyprland, Wayland, PipeWire, MPRIS, and Polkit modules. Their QML APIs and session behavior need a live test. Its network panel uses a NetworkManager backend while this Pi intentionally retains `systemd-networkd` and `wpa_supplicant`.
- `default/hypr/autostart.lua` calls power-profile initialization and `udiskie`, but those helpers remain deferred. `config/hypr/xdph.conf` names the deferred `hyprland-preview-share-picker`; screen sharing needs a tested fallback or revised setting.
- Deferring Nautilus temporarily leaves the stock graphical file-manager action without its target. A safer file manager or a separately reviewed Nautilus transaction is needed before claiming full desktop parity.
- Audio user services, portal activation, Quickshell's Polkit agent, the terminal/font pairing, and the graphical session were not run on the Pi.

## Policy changes from this audit

The package policy now defers `nautilus`, replaces `vi` with `ex-vi-compat`, excludes `egl-wayland`, and identifies `avahi` as an explicit candidate because it enters through `pipewire-pulse`. No packages were installed while making these source-tree changes.

Before any install, refresh and re-resolve package metadata as a separate reviewed operation, inspect the exact package archives/file lists and install scripts against the Pi's hooks, then produce an allow-listed transaction with backups and rollback for the encrypted boot and existing network/SSH path. The minimal session also needs its missing user-facing helpers handled explicitly. See the [compatibility guide](arm64.md) for the deployment boundary.
