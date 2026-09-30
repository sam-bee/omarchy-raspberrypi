# Omarchy for Raspberry Pi 5

An experimental Raspberry Pi 5 installer for the Omarchy Quattro desktop. Boot the USB installer, connect over SSH or Remote Desktop, and install onto your Pi's NVMe SSD or microSD card. The same USB provides boot repair afterwards.

Omarchy is created by DHH and the [Omarchy project](https://omarchy.org). This independently maintained ARM64 port follows the upstream desktop where the hardware supports it. See the [Omarchy manual](manual/01-welcome-to-omarchy.md) for desktop use.

## Experimental Compute Module 5 test image

The planned [CM5 test release v0.1.1-cm5-test-2026.09.30](https://github.com/sam-bee/omarchy-raspberrypi/releases/tag/v0.1.1-cm5-test-2026.09.30) is for experimental bring-up. A USB installer boot has been photographed on a CM5 and showed 119.1 GiB MMC and 238.5 GiB NVMe storage. Installation and first boot from an installed target remain untested, so this does not establish CM5 hardware acceptance.

Use the [CM5 test release note](release/omarchy-cm5-test-2026.09.30/RELEASE.md) for the test scope and checklist. When the planned release is published, use its image assets and checksums instead of the stable Pi 5 assets in this guide. The CM5 must already have EEPROM boot order configured to try USB before the selected NVMe or MMC target; the installer does not change boot order. Confirm which device is the approved target, keep the other storage protected, boot the installer from USB, and remove the installer USB before testing the installed target's first boot.

## What you need

- A Raspberry Pi 5 with 8 GB RAM, suitable power supply, and NVMe SSD or microSD installation target; use 32 GB or larger for the target.
- A USB installer stick of at least 16 GB, and another computer to flash and configure it.
- Ethernet or Wi-Fi, internet access during installation, and an SSH or RDP client on the same network.
- For encrypted automatic boot: a separate USB stick that the installer can erase and turn into an unlock key. Keep the recovery passphrase too.

The Pi must already boot USB before its installation target. The installer preserves the EEPROM boot order. Disconnect other storage you do not need during installation.

## 1. Download and verify

Download all three numbered image parts and the checksum file from the [Pi installer release](https://github.com/sam-bee/omarchy-raspberrypi/releases/tag/v0.1.0-pi5-2026.09.29):

- [Image part 00](https://github.com/sam-bee/omarchy-raspberrypi/releases/download/v0.1.0-pi5-2026.09.29/omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img.zst.part-00)
- [Image part 01](https://github.com/sam-bee/omarchy-raspberrypi/releases/download/v0.1.0-pi5-2026.09.29/omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img.zst.part-01)
- [Image part 02](https://github.com/sam-bee/omarchy-raspberrypi/releases/download/v0.1.0-pi5-2026.09.29/omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img.zst.part-02)
- [SHA256SUMS](https://github.com/sam-bee/omarchy-raspberrypi/releases/download/v0.1.0-pi5-2026.09.29/SHA256SUMS)

Put the four files in the same folder and verify the parts before joining them:

```sh
sha256sum --ignore-missing --check SHA256SUMS
```

Each of the three image parts must report `OK`. On macOS, use `shasum -a 256 -c SHA256SUMS` and check each part's result; missing metadata files are harmless if you downloaded only the image parts. On Windows, use `Get-FileHash .\*.part-* -Algorithm SHA256` in PowerShell and compare each part's hash with its entry in `SHA256SUMS`.

Join the parts in order, then decompress the image on Linux or macOS (requires `zstd`):

```sh
cat omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img.zst.part-00 omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img.zst.part-01 omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img.zst.part-02 > omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img.zst
zstd --decompress --keep omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img.zst
```

On Windows, join them in **Command Prompt**:

```bat
copy /b omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img.zst.part-00+omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img.zst.part-01+omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img.zst.part-02 omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img.zst
```

Then extract the resulting `.img.zst` with an archive application that supports Zstandard. Allow at least 25 GB of free disk space for the downloaded parts, joined archive and extracted `.img`. You may delete the numbered parts once joining and decompression succeed.

## 2. Flash and configure the USB

Use [Raspberry Pi Imager](https://www.raspberrypi.com/documentation/computers/getting-started.html) or another disk-image writing application to write `omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img` to the **whole USB stick**. This erases that stick; check its capacity and model before starting. Copying the `.img` onto an existing filesystem will not make a bootable installer.

In Raspberry Pi Imager, choose **Use Custom** for the OS, select the extracted `.img`, and select your USB as the storage device. Skip Imager's OS customisation; this installer uses the settings file below. Let writing and verification finish.

After flashing, open the USB's FAT boot partition on your computer. Copy `installer-settings.example.toml` to `installer-settings.toml` in that partition's top-level folder. Edit it as plain text before the first boot. Here is a complete Wi-Fi example; replace every password and the network name with your own values:

```toml
[installer]
hostname = "omarchy-pi"
username = "omarchy"

[wifi]
country = "GB"
ssid = "your-network-name"
password = "your-wifi-password"

[ssh]
password = "choose-an-installer-login-password"

[rdp]
password = "choose-a-separate-rdp-password"
```

Use your uppercase two-letter Wi-Fi country code. For Ethernet, omit the entire `[wifi]` section. Passwords must have at least eight characters. For SSH key login, supply `authorized_key = "<your complete OpenSSH public key>"` in `[ssh]`; it can replace the SSH password. RDP still needs its own password. Keep TOML string values quoted; literal single quotes are useful for passwords containing double quotes or backslashes. Save the exact filename `installer-settings.toml`, without an added `.txt` extension, and safely eject the USB.

These settings configure the installer account. You will choose the installed desktop's account and password separately.

## 3. Boot and connect

With the Pi powered off, attach the installer USB, the installation target and, if needed, the separate disposable key USB. Connect Ethernet if you selected it, then power on. Networking, SSH and the RDP installer desktop start automatically.

Find the installer's IP address in your router's connected-device or DHCP list, using the hostname you configured. Connect with either:

- **Remote Desktop:** connect to that IP on port `3389`, using the configured installer username and the `[rdp]` password. The installer terminal opens automatically.
- **SSH:** run `ssh -t omarchy@<installer-ip> omarchy-pi-install`, replacing the username if you changed it. Use the `[ssh]` password or your configured SSH key.

If the installer cannot connect, power off, return the USB to your computer and check the settings filename and Wi-Fi entries, or configure Ethernet. If account or password settings need changing after provisioning has started, reflash and configure the USB before trying again.

## 4. Install

1. Choose **Install Omarchy on a target**. Select the intended SSD or microSD by its model, capacity and identity. The installer USB and existing unlock-key media are protected from selection.
2. Enter the new desktop account, password, hostname and regional settings. Review Wi-Fi and remote access for the installed system. If you want to continue connecting directly by RDP, select LAN RDP access.
3. Choose encryption. **No disk encryption** needs no key stick. **Encrypted: passphrase at boot** needs local input to unlock before normal networking starts. **Encrypted: separate USB key** prepares the separately selected stick and also asks for a recovery passphrase. Save that passphrase.
4. Review the settings and type the exact erase confirmation shown for the target. Preparing a new key stick requires its own confirmation and erases that stick too.
5. Wait for completion. Installation runs independently of the terminal: after an SSH/RDP disconnection, reconnect and run `omarchy-pi-install` to view the same job. A failed or interrupted installation offers a separately confirmed restart from the beginning.

## 5. Boot your new desktop

Read the completion card and note the new hostname, username, SSH fingerprint and access instructions. Choose the installer's shutdown action. Once the Pi has powered off, remove the installer USB. For automatic encrypted unlock, leave the new key USB attached. Power on again.

Find the installed system's address in your router; it may differ from the installer's address. SSH uses the new desktop username and password/key. RDP uses the installed system's selected access mode and RDP password. Compare the SSH fingerprint with the completion card when connecting for the first time.

Keep the configured installer USB for recovery.

## Updates and recovery

Run `omarchy update` as the desktop user, then reboot after it completes. If you disconnect, `omarchy update --status` shows the result and `omarchy update --watch` reconnects to progress. See [Pi updates](docs/arm64.md#pi-updates).

For boot repair, power off, attach the configured installer USB and boot it. Connect as before and choose **Repair target boot files**. Identify the target, unlock it with its attached key USB or recovery passphrase, review the proposed repair and confirm. After completion, shut down and remove the installer USB again. See [USB recovery commands](docs/arm64.md#usb-recovery-commands) for the command-line procedure. Repair preserves the installed desktop; choosing Install again erases its target after confirmation.

## Build and source

Release assets include checksums, package/version records and build provenance. The [image builder guide](install/arm64/installer-image/README.md) describes building an installer; the [ARM64 guide](docs/arm64.md) covers the port's implementation and maintenance. Development takes place on `quattro-rpi5`.

## License

Omarchy source is released under the [MIT License](LICENSE). Bundled operating-system packages and firmware retain their respective licenses; see the release's license information.
