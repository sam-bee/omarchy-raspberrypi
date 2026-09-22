# Local archive review — 23 September 2026

This is a partial content review, not package-signature verification or permission to install. On the workstation, 105 archives matched the SHA-256 values retrieved from the Pi's cached sync databases. Luna 6 xhigh independently inspected their actual metadata, file lists, install scripts and hooks. The missing `bubblewrap 0.12.0-1` archive returned 404; it is a hard dependency through `hyprland → hyprcursor → librsvg → gdk-pixbuf2 → glycin → bubblewrap`. Do not omit it or bypass dependencies. Re-resolve fresh metadata and review any changed closure.

| Package(s) | Reviewed installed effect |
| --- | --- |
| `libinput`, `libwacom` | Three udev rules: `80-libinput-device-groups.rules`, `90-libinput-fuzz-override.rules`, `65-libwacom.rules`. These trigger the installed system udev hook, whose global device re-trigger requires the narrowly scoped guard in the plan. |
| `fontconfig` | `.INSTALL` runs the fontconfig configuration helper and `fc-cache -rs`; `/etc/fonts/conf.d` symlinks and font caches are created. Ships configuration/cache hooks. |
| `gdk-pixbuf2` | Ships loader-cache hook; `.INSTALL` removes its loader cache on package removal. |
| `shared-mime-info` | Ships MIME database/cache hooks; writes `/usr/share/mime` cache state. |
| `seatd` | Ships `seatd.service` and sysusers declaration `g seat - -`, so the existing sysusers hook can create group `seat`. Do not add Sierra to it. |
| `lm_sensors` | Ships `fancontrol`, `healthd`, `lm_sensors`, `sensord` system units. None is to be enabled or started, including fan control on this water-cooled Pi. |
| `foot` | Ships user `foot-server.service` and `.socket`; the smoke launches a standalone terminal, not either unit. |

The 105 inspected archives contain only the two `.INSTALL` files listed above, no service presets and no tmpfiles files. None has a reviewed install script that enables a service. Existing system hooks can perform daemon reloads, sysusers and cache updates. Their current target-specific behavior remains part of the final transaction review.

No path in the 105 inspected archive inventories matches the Pi's recorded `90-mkinitcpio-install.hook` targets for initcpio, firmware, external kernel modules, DKMS, systemd/udevd, cryptsetup/LVM/mdadm, the named crypto/PCSC libraries or modprobe configuration. This does not cover the missing archive, future package changes, arbitrary script behavior, or unreviewed hooks. Recheck both installation and removal after fresh resolution.

The temporary archive inventory is on the workstation under `/tmp/omarchy-pi-archive-review/`; it is disposable, not a durable deployment cache. The committed manifest retains the exact reviewed name/version/URL/hash snapshot. A later deployment must retain its verified archives and review evidence in its private run record.
