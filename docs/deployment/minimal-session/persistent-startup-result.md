# Persistent startup and postboot RDP result

**Result: pass for the tested Raspberry Pi 5 state on 23 September 2026.** The system tty8 unit is enabled and started the encrypted-NVMe installation unattended after reboot. A real FreeRDP client connected through the SSH tunnel, accepted keyboard and pointer input, and a second client reconnected to the same running graphical session. No package update was applied.

## Boot and session evidence

- The boot ID changed from `fb39fd58-b51e-48eb-b818-0c05d8a17377` to `689027ee-0a2a-4f29-a5d3-989ff6786186` at 12:09 UTC.
- The root filesystem was mounted from the encrypted NVMe mapper and `/boot` from its NVMe FAT partition. SSH and Wi-Fi returned. The boot-enabled `omarchy-pi-uwsm-session.service` was active with MainPID 563; its UWSM wrapper was PID 645 and Hyprland PID 650.
- `omarchy-pi-hypr-rdp.service` was active as PID 676 and listened only on `127.0.0.1:3389`. The expected tty8 PAM session and graphical session were present. No failed units were reported.
- The repaired glslang library retained SHA-256 `12b4359e15f10a35d3206c1ac9dfe808dd21d23fb176f5c132cae30e649f17e6`; `pacman -Qkk glslang` reported 53 files checked and zero altered. Package state was 347 total: 35 explicit and 312 dependencies. The pacman database integrity check was clean.
- The RDP TLS certificate and key hashes matched the accepted manual-trial values across the reboot. Hash values are retained in private run evidence.

## Recovery baseline

The encrypted baseline `20260923T120330Z` was created after the manual persistent-service trial and before the controlled reboot. It decrypted and passed an independent protected-state comparison. The comparison covered package versions and install reasons, protected file hashes and path inventory, LUKS header, boot files, network and service state, persistent-session files, and RDP/build state. No package transaction was part of this result.

## Real client acceptance

Two FreeRDP clients connected after the reboot via the local SSH tunnel. The first client showed live Omarchy frames after Foot was opened; its initial frames were blank. A keyboard marker was entered and observed, and a pointer action changed the workspace. After client one closed, client two reconnected to the same Hyprland PID 650 and RDP server PID 676, then repeated keyboard and pointer checks. The listener remained loopback-only. Private workstation captures and their manifest are retained under the local RDP harness state directory as `postboot-client-acceptance-manifest.txt`; no screenshot or credential material is stored in this repository.

## Limits and next maintenance gate

This proves unattended encrypted boot, the tty8/UWSM/Hyprland/RDP service path, and client reconnection for this recorded software and hardware state. It is not evidence that a package upgrade succeeds or remains bootable. The refreshed scratch full-upgrade preview returned zero candidates; its repository databases lacked signatures, and no package archives or update transaction were applied. Before maintenance, repeat the current-state checks, refresh scratch repository metadata, create a fresh encrypted recovery baseline, and review and verify the signatures of every package in a complete candidate transaction if one exists.
