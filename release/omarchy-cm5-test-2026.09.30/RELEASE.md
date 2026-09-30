# Omarchy CM5 experimental installer test — 2026-09-30

This is an experimental Raspberry Pi Compute Module 5 installer image for bring-up and guarded installation testing. The current evidence shows USB boot far enough to display two storage devices; it does not establish successful CM5 installation, target boot, first-boot desktop operation, or supported hardware status.

## Image and provenance

- Raw image (reconstructed locally): `omarchy-cm5-0.1.1-cm5-test-2026.09.30-installer.img`; 14,643,363,840 bytes; SHA-256 `ff5e6c75e99de491d7ea06ba49a6905c9adeb6dd0aafe161170d1e228e791b3e`
- Reconstructed zstd stream: `omarchy-cm5-0.1.1-cm5-test-2026.09.30-installer.img.zst`; 4,628,688,958 bytes; SHA-256 `3a8832738444040f34ab556fd174df8b7e2aae3b7f62fc27d23eb7b3c27f0a97`
- Installer runtime source: `7867d04893d472219bc32ab034dce0e6e5b375fc`
- Installer runtime digest: `cfbf09cc2363ebe196e75dad56fc1b91b4ca597148b2593c9788b29b51861ac2`
- Installer package records: 235; package manifest SHA-256 `65051a142edee7aecb63e7b3f97166d4a31dc262c871db717a9a0892bbedda88`
- Desktop payload source: `fcb2b5afcb8c06b6daf93319792f072f9111d669`
- Desktop payload: 633 installed package records; 2,800,963,177 bytes; SHA-256 `1f640ef4641cf0a2086956d9b7c51368b5d95a9a853b31bf4dfcd27c1637042c`
- Build verification: root UUID `fb6f6abc-42d7-4ebc-8488-fe9829d73283`, boot UUID `34F8-E775`, kernel `kernel8.img`

The source image is split into three release assets because GitHub release assets must remain below 2 GiB:

- `omarchy-cm5-0.1.1-cm5-test-2026.09.30-installer.img.zst.part-00` — 1,900,000,000 bytes; SHA-256 `c47c985793132793b52dfde0233c64ff78a8188fa0caf7b3e4fa2f21ae8987ee`
- `omarchy-cm5-0.1.1-cm5-test-2026.09.30-installer.img.zst.part-01` — 1,900,000,000 bytes; SHA-256 `61dbcd58e6b775a470394070527e00860a707e7b3d0235e448f6a1f9d046d589`
- `omarchy-cm5-0.1.1-cm5-test-2026.09.30-installer.img.zst.part-02` — 828,688,958 bytes; SHA-256 `f1b1faca4e3ff8a8fd4b7b01c1b34869b554c90850b516684f201f52fa82ec3d`

The raw image and unsplit stream are recorded for provenance but are not release assets.

## Reconstruct and verify the image

Download the three `.part-*` assets and `SHA256SUMS` into one directory:

```sh
sha256sum --ignore-missing --check SHA256SUMS
cat omarchy-cm5-0.1.1-cm5-test-2026.09.30-installer.img.zst.part-00 \
    omarchy-cm5-0.1.1-cm5-test-2026.09.30-installer.img.zst.part-01 \
    omarchy-cm5-0.1.1-cm5-test-2026.09.30-installer.img.zst.part-02 \
    > omarchy-cm5-0.1.1-cm5-test-2026.09.30-installer.img.zst
sha256sum omarchy-cm5-0.1.1-cm5-test-2026.09.30-installer.img.zst
zstd --test omarchy-cm5-0.1.1-cm5-test-2026.09.30-installer.img.zst
zstd --decompress --keep omarchy-cm5-0.1.1-cm5-test-2026.09.30-installer.img.zst
sha256sum omarchy-cm5-0.1.1-cm5-test-2026.09.30-installer.img
stat -c '%n %s bytes' omarchy-cm5-0.1.1-cm5-test-2026.09.30-installer.img.zst omarchy-cm5-0.1.1-cm5-test-2026.09.30-installer.img
```

The reconstructed stream must hash to `3a8832738444040f34ab556fd174df8b7e2aae3b7f62fc27d23eb7b3c27f0a97` and the decompressed image must hash to `ff5e6c75e99de491d7ea06ba49a6905c9adeb6dd0aafe161170d1e228e791b3e` with `14,643,363,840` bytes. Write the resulting `.img` to the whole approved installer USB only after the separate hardware and media checks are complete.

## Build reference

The installer runtime was built from `7867d04893d472219bc32ab034dce0e6e5b375fc` on native aarch64 Pi hardware using the signed Arch Linux ARM rootfs and the separately pinned desktop payload. The [image builder at the pinned source revision](https://github.com/sam-bee/omarchy-raspberrypi/blob/7867d04893d472219bc32ab034dce0e6e5b375fc/install/arm64/installer-image/build-installer-image.py) and its [build guide](https://github.com/sam-bee/omarchy-raspberrypi/blob/7867d04893d472219bc32ab034dce0e6e5b375fc/install/arm64/installer-image/README.md) record the plan-first, fresh-workdir, `--apply` workflow.

## Test scope

The next test is to use the existing Pi installer path on a CM5 and record whether it can safely install to an explicitly chosen target and boot that target afterwards. Keep the [installer image guide](../../install/arm64/installer-image/README.md) and the [user installation guide](../../README.md) alongside this note; they define the image preparation, USB configuration, installer workflow and recovery path.

The installer reads but does not change the Raspberry Pi EEPROM boot order. It requires USB first (mode `4` or, on a suitable CM5 USB port, mode `5`) and a later mode for the selected target: `6` for NVMe or `1` for MMC/SD/eMMC. Check the existing order with `vcgencmd bootloader_config`. If it lacks the target fallback, decide how to configure the CM5 before installation; the installer will refuse that target until the order is usable. Boot with the configured installer USB attached, select the target only after checking its model, capacity and identity, and remove the installer USB before the first boot of the installed system so the target is exercised.

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
