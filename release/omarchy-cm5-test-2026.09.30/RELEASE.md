# Omarchy CM5 experimental installer test — 2026-09-30

This is an experimental Raspberry Pi Compute Module 5 installer test record. A photograph shows the USB installer booting far enough to display two storage devices: 119.1 GiB MMC and 238.5 GiB NVMe. No CM5 installation, target write, reboot into an installed system, or first-boot desktop test has been completed. This evidence does not establish CM5 hardware acceptance or a supported release.

## Test scope

The next test is to use the existing Pi installer path on a CM5 and record whether it can safely install to an explicitly chosen target and boot that target afterwards. Keep the [installer image guide](../../install/arm64/installer-image/README.md) and the [user installation guide](../../README.md) alongside this note; they define the image preparation, USB configuration, installer workflow and recovery path.

The installer preserves the Raspberry Pi EEPROM boot order. The CM5 must already be able to try USB before its installation target. Do not change EEPROM boot order for this test. Boot with the configured installer USB attached, select the target only after checking its model, capacity and identity, and remove the installer USB before the first boot of the installed system so the target is exercised.

## USB preparation

Write the complete installer image to the whole approved USB stick with Raspberry Pi Imager or an equivalent image writer, then let the write verification finish. Skip Imager OS customisation. Mount the USB FAT boot partition, copy `installer-settings.example.toml` to the exact name `installer-settings.toml`, fill in the installer hostname, account, network and SSH/RDP access settings, then safely eject the USB. The [USB configuration section](../../README.md#2-flash-and-configure-the-usb) has the complete settings format. Do not put private passwords or keys in this release note.

## Protected disk choice

The photograph proves that the installer can see both an MMC device of 119.1 GiB and an NVMe device of 238.5 GiB. It does not prove which physical device is safe to erase. Treat both as protected until the operator confirms the intended disposable target by model, capacity and identity. If the NVMe is the approved test target, select only that confirmed 238.5 GiB device and leave the 119.1 GiB MMC untouched; reverse those instructions only when the operator has explicitly designated the MMC. Do not select the installer USB or any existing unlock-key USB. The installer’s selection guards are useful evidence, but the operator must still review the final erase confirmation.

## First CM5 installation checklist

1. Record the CM5 model string, firmware version, installer image identity and the pre-test storage inventory. Keep the installer USB and an independent recovery connection available.
2. Boot from USB and confirm the installer reaches its terminal or remote access path. Record the model and storage output again from the running installer, including device names, capacities, models, transport and mount points.
3. Start the installer, identify the intended target by more than its size, and confirm that the installer USB and any unlock-key media cannot be selected. Stop if the UI presents an ambiguous device.
4. Run one installation using the approved target and record the selected layout, encryption choice, completion result and any installer errors. Keep the recovery passphrase when encryption is enabled.
5. Shut down through the installer, remove the installer USB, and boot the CM5 from the installed target. Leave an approved automatic-unlock key attached only when that mode was selected.
6. On first boot, verify the model, kernel, root and boot mounts, storage layout, network, fresh SSH access and the selected RDP mode. If the graphical session starts, check the display, Hyprland configuration errors, Quickshell and a terminal window.
7. Check that the unselected storage device remains unchanged and that the installer USB still boots as a recovery medium. If any check fails, preserve logs and stop before another install attempt.

## Evidence to report

Report the exact CM5 model and firmware, image filename and source revision, USB preparation result, pre-install and installer-side `lsblk` inventory, the target identity selected, the install result, and the post-install `findmnt`, kernel, network, SSH, RDP and graphical-session results. Include the completion screen and target-selection evidence where practical, plus the first-boot logs or failure messages. Redact passwords, Wi-Fi credentials, private keys, recovery passphrases, host keys and other secrets.

CM5 hardware acceptance requires a successful target installation and first boot with the protected-device checks recorded. The current USB boot photograph is an encouraging bring-up observation only; it is not that acceptance evidence.
