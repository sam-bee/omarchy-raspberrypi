# Unattended encrypted reboot result — 23 September 2026

**Result:** the Raspberry Pi 5 rebooted after the 329-package minimal-session stage and returned over fresh SSH without physical interaction. The current boot journal records the configured USB key device mounting before `cryptroot` was opened. This proves unattended boot for this package and boot state; it does not prove persistent graphical login or RDP.

## Recovery gate and starting state

The pre-reboot boot ID was `c8cc313f-0436-4ce3-a164-858e180ceae5`. The Pi had 329 packages (30 explicit, 299 dependencies), an encrypted ext4 root at `/dev/mapper/cryptroot`, a separate NVMe FAT32 `/boot`, active SSH/networkd/Wi-Fi, and `graphical.target` as its existing default. No graphical session was running. A new root-only recovery baseline at `/var/lib/omarchy-pi-deploy/20260923T080310Z` captured this exact 329-package state, protected system and boot files, LUKS metadata/header, USB key hash, EEPROM, package archives/signatures, and Sierra's staged session. Its encrypted off-Pi copy was retained outside the repository and synced workspace; the local ciphertext hash matched its manifest and a full GPG decrypt piped into `tar -tf -` succeeded. The recovery archive is selective evidence, not a full disk image.

The first encryption attempt created Sierra's previously absent `.gnupg` directory. Before reboot, it contained only an empty `private-keys-v1.d`, `common.conf`, and `random_seed`; the five GPG agent units were inactive and no agent process remained. This exact generated directory was accepted as a recorded pre-reboot exception. No key was created there and no boot, package, or network configuration was changed by the backup attempt.

## Reboot and fresh connection

The controlled `systemctl reboot` was issued at approximately 08:29 UTC. The prior SSH connection closed normally. A fresh, strict-host-key SSH login returned within approximately 15 seconds with boot ID `fb39fd58-b51e-48eb-b818-0c05d8a17377` and an uptime under one minute. Nobody entered a disk passphrase or interacted with the physical Pi. The current boot journal shows the kernel command line selecting `cryptroot` and the USB key UUID, the key device mounted at about 2.9 seconds, and cryptsetup finished by about 7.0 seconds. The encrypted root was mounted from `/dev/mapper/cryptroot`; `/boot` remained `/dev/nvme0n1p1`.

After reboot the kernel remained `6.18.52-1-rpi`. Fresh SSH found 329 packages with 30 explicit and 299 dependency reasons, `pacman -Dk` clean, and exact package version/reason matches against the new baseline. Active and enabled `sshd`, `systemd-networkd`, and `wpa_supplicant@wld0` returned; the Pi retained `192.168.178.21/24` on Wi-Fi and a default route through `192.168.178.1`. The default target remained `graphical.target`. No Wayland compositor or graphical-session user unit was active.

Root-privileged comparisons passed for the baseline's protected hashes, boot-critical hashes, USB key hash, LUKS JSON metadata, EEPROM configuration, 25 recorded symlink targets, the recorded absent path, and the enabled-unit list. The root filesystem had about 428 GiB free. The extra independent post-reboot audit did not complete because its credential probe stopped before the audit commands; the checks above were run in separate fresh SSH sessions by the operator. No claim is made for checks that the independent attempt did not perform.

## Next gate

Keep SSH as the recovery path. Before adding RDP build dependencies, resolve a complete Arch Linux ARM upgrade against fresh metadata in a scratch pacman database. A nonempty full-upgrade preview blocks a narrow build-tool install, because that would create a partial rolling-release upgrade. After a reviewed package transaction, take a new exact-state backup and repeat the protected-state checks. A manual RDP client must show advancing live frames, keyboard and pointer input, and a successful reconnect before persistent graphical startup is enabled.
