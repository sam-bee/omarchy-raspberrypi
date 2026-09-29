# Raspberry Pi 5 installation rehearsal

This is the step 6 acceptance procedure for a freshly flashed Omarchy Pi 5 installer image. Use a deliberately disposable target disk and, for the encrypted key test, a separate disposable USB key. Keep any existing system disk and its recovery media disconnected or otherwise protected. If an existing bootable disk remains attached, do not assume that the new SD target will be selected automatically; use the operator-approved temporary firmware boot selection method when needed. The installer never changes the Pi's EEPROM boot order.

The procedure exercises the complete path: configure the installer image, connect over SSH and direct RDP, install under a new username, cancel before submission, detach and reconnect while the worker runs, boot the installed desktop, update it, and repair its boot files from USB with an attached unlock-key USB or the recovery passphrase.

## Flash and prepare the installer image

Start with the candidate image and its accompanying `SHA256SUMS` file. Verify the supplied file before writing it:

```bash
sha256sum --check --ignore-missing SHA256SUMS
```

Use the candidate archive filename listed by the accompanying `SHA256SUMS`. Decompress it to the raw image without deleting the archive, then check the raw image against its checksum too:

```bash
zstd -dk omarchy-step6-installer-candidate.img.zst
sha256sum --check --ignore-missing SHA256SUMS
```

Replace `omarchy-step6-installer-candidate.img.zst` with the exact candidate archive filename from `SHA256SUMS`.

Before writing, unplug protected removable media and inventory the remaining devices by stable identity:

```bash
lsblk -d -o PATH,SIZE,MODEL,SERIAL,TRAN
ls -l /dev/disk/by-id/
```

Use a verified image writer such as GNOME Disks (**Restore Disk Image**) or Raspberry Pi Imager's custom-image flow. Select the whole device whose displayed serial and capacity match the designated installer USB; never select a partition or guess a `/dev/sdX` name. Let the writer finish and flush the raw `.img` write; use its verification option when available. Keep the installer USB attached for the settings step below, then eject it before moving it to the Pi.

Mount the installer's FAT boot partition on another computer and copy `installer-settings.example.toml` to `installer-settings.toml` at the partition root. Edit the new file with the installer account and connection details:

```toml
[installer]
hostname = "omarchy-pi"
username = "installer"

# Omit the complete [wifi] table when using Ethernet.
[wifi]
country = "GB"
ssid = "your-network-name"
password = "your-wifi-password"

[ssh]
# Use one or both. An authorized_key is one complete OpenSSH public key.
authorized_key = "ssh-ed25519 AAAA..."
# password = "your-installer-login-password"

[rdp]
password = "your-installer-rdp-password"
```

The `[installer]`, `[ssh]`, and `[rdp]` tables are required. `[wifi]` is optional; when it is omitted, use wired networking. SSH needs either `authorized_key` or `password`. RDP uses the installer username and the separate `[rdp]` password on port 3389. Do not copy a private key into the image or repository. The target account, target password, hostname, timezone, locale, and keyboard layout are entered later in the TUI; they are not silently taken from this file.

Insert only the installer USB, the designated disposable target, and (for the encrypted-key run) a blank disposable key USB. The key USB must be blank and unpartitioned before the installer starts; the installer refuses partitioned key media. Record each physical device's serial and capacity before powering on. New sticks may arrive partitioned too. If necessary, use a disk utility to delete all partitions and filesystem signatures, then recheck the stick’s serial and capacity; this is destructive preparation and must target only the intended disposable key.

## Connect to the installer

Boot the Pi from the installer USB and wait for its configured network to obtain an address. If the Pi selects another attached bootable disk, arrange a temporary boot selection with the operator; the installer does not change the permanent boot order. Find that address in the router's DHCP/device list. Try the configured hostname when the router's DNS resolves it, then fall back to the DHCP address; the image does not advertise a hostname by itself. Verify the installer SSH host-key fingerprint on the first connection, then run the guided installer with an interactive terminal:

```bash
ssh -t <installer-username>@<installer-address> omarchy-pi-install
```

When router DNS resolves the hostname, the same command can use it:

```bash
ssh -t <installer-username>@<hostname> omarchy-pi-install
```

The `-t` is required; without a TTY the curses frontend refuses to run. The installer desktop starts automatically as well. Connect an RDP client directly to `<installer-address>:3389` using the installer username and the `[rdp]` password, then use the Foot terminal there to run `omarchy-pi-install` if the TUI is not already visible. Confirm that both SSH and direct RDP reconnect successfully before submitting an installation.

## Install the encrypted target

Choose **Install Omarchy on a target**. The disk chooser shows model, size, and unavailable reasons. Select only the disposable target disk. The installer refuses the running installer, mounted disks, and protected or unsuitable media; do not proceed if the displayed identity is not the disk you recorded.

Complete the sections as follows:

1. In **Account & regional**, enter a new target username and password, hostname, timezone, locale, and keyboard layout.
2. In **Network**, choose **Configure Wi-Fi** and enter the target's country, SSID, and password when Wi-Fi is needed. The installer may offer the installer Wi-Fi values for explicit reuse. Wired networking can be left unconfigured.
3. In **Remote access**, enable SSH. The authorized SSH public key is optional; leave it blank to use the target account password, or provide one complete OpenSSH public key for key login. Select **LAN (direct client)** for direct target RDP and enter a target RDP password; **Loopback (SSH tunnel)** is an alternative when RDP should remain local to SSH.
4. In **Encryption**, choose **Encrypted: separate USB key**, enter and confirm a recovery passphrase, and select the separate blank, unpartitioned key USB when prompted. Never select the installer USB or an existing recovery key.
5. Choose **Review and continue**. Check the target and key identities, settings, and the payload details. Type the exact target token, then the exact key token, and type `YES` to consent to the installer's required downloads.

Before the final token and `YES` confirmations, Esc or **Back** cancels without changing a disk. A wrong token or declined internet consent also leaves the disks unchanged. After submission, do not start another job or remove power. The install runs as a background service; the progress screen is only an observer.

## Disconnect and reconnect

After the job reaches a progress phase, press `q` or Esc to leave the progress view, or close the SSH/RDP client. This detaches the viewer but does not stop the worker. Reconnect and run the same command:

```bash
ssh -t <installer-username>@<installer-address> omarchy-pi-install
```

The installer home screen finds the active job and resumes its progress view. **View latest job** reads the persisted result after completion. If a job is failed or interrupted, the installer requires the exact phrase `RESTART FROM SCRATCH` before beginning a new installation; it does not resume an incomplete destructive phase automatically.

When installation completes, read the completion card and record the new target SSH fingerprint. Choose **Shut down installer** only when the job is idle or complete, type `SHUT DOWN INSTALLER`, and wait for power-off. Remove the installer USB. Leave the new key USB inserted for key-unlocked encrypted boot, then power on the Pi. If another bootable disk remains attached and is selected instead, arrange temporary target selection with the operator; do not expect the SD target to be chosen automatically. For passphrase-unlocked encryption, enter the recovery passphrase at the local boot prompt before expecting SSH or RDP.

## Boot and use the target

Find the target's new DHCP address and verify the completion-card fingerprint before the first SSH login. Try `<target-hostname>` if the router resolves it, then fall back to the DHCP address:

```bash
ssh <target-username>@<target-address>
```

For direct RDP, connect to `<target-address>:3389` with the target username and target RDP password. For loopback RDP, create the tunnel and connect the RDP client to the local endpoint:

```bash
ssh -L 3389:127.0.0.1:3389 <target-username>@<target-address>
```

Use the normal Omarchy launcher, terminal, browser, file manager, theme picker, and audio/session controls available in the image. Check that the selected target settings and the external-key boot survive a reboot. Keep the installer and key USB identities separate throughout the rehearsal.

## Update, then exercise USB recovery

From the installed target, run the normal update as the desktop user, never with `sudo`:

```bash
omarchy update
```

While it is running, disconnect the SSH client. Reconnect and inspect or follow the same persisted job:

```bash
omarchy update --status
omarchy update --watch
```

Allow the update to finish and reboot when it reports that a reboot is required. Confirm that the target returns with its SSH/RDP access, desktop, configuration, and external-key boot intact. A source rollback, if needed for the rehearsal, is separate from package rollback and does not undo the package transaction.

For recovery, power down and boot the installer USB again. If the Pi selects another attached bootable disk, arrange temporary USB selection with the operator; the installer never changes permanent boot order. Keep the encrypted target connected. Start with a read-only inventory:

```bash
omarchy-pi-recover discover
omarchy-pi-recover status
```

Use the exact `RECOVER ...` token printed for the target. The discovery response also lists eligible attached unlock-key partitions with their partition path, parent stable ID (including serial when available), filesystem UUID, size/model, and an exact key token. When two key USB sticks are attached, match both the parent serial/stable ID and filesystem UUID to the physical stick you recorded; do not select a path by size alone. Leave the key USB attached but unmounted; do not open it in the file manager before discovery. If the desktop auto-mounted it, eject or unmount it in the file manager and run discovery again. The guided **Repair target boot files** flow offers **Use attached unlock-key USB**, then a chooser that displays those identities and a token confirmation. The installer controller performs the temporary read-only key access itself; this flow does not require the installer account to mount anything or use sudo.

The command-line attached-key flow uses the partition path and exact token returned by `omarchy-pi-recover discover`:

```bash
omarchy-pi-recover plan \
  --target /dev/<target-disk> \
  --confirm-target '<target token from discover>' \
  --key-device /dev/<key-partition> \
  --confirm-key '<key token from discover>'

omarchy-pi-recover inspect \
  --target /dev/<target-disk> \
  --confirm-target '<target token from discover>' \
  --key-device /dev/<key-partition> \
  --confirm-key '<key token from discover>'

omarchy-pi-recover repair \
  --target /dev/<target-disk> \
  --confirm-target '<target token from discover>' \
  --key-device /dev/<key-partition> \
  --confirm-key '<key token from discover>' \
  --confirm-repair
```

The existing `--key-file` option remains a fallback for a trusted root setup that has already supplied a read-only-mounted, root-owned key file with no group/world permissions; the ordinary installer-account flow should use the attached-key option above.

The passphrase route needs no key USB during recovery. Remove the key USB, then repeat the plan, inspection, and repair using the existing recovery passphrase. `--passphrase` prompts without putting it in the command line:

```bash
omarchy-pi-recover repair \
  --target /dev/<target-disk> \
  --confirm-target '<token from discover>' \
  --passphrase \
  --confirm-repair
```

Use `omarchy-pi-recover status --watch` to follow the repair job. Before booting an encrypted target unattended, reinsert the new unlock-key USB; otherwise keep it absent and answer the local boot prompt with the recovery passphrase. Remove the installer USB and boot the repaired target. Recovery planning and inspection are read-only. Confirmed repair is limited to the installed boot files and initramfs; it does not reinstall the system, change LUKS keyslots, change the EEPROM boot order, or provide a full-system rollback.

## Current rehearsal limits

The step 6 rehearsal needs a checksum-identified candidate image and a designated disposable target/key pair. Release publication and download hosting remain step 7, but the candidate can be verified and written with the generic image-writer procedure above. Attached-key recovery is the supported no-sudo flow; explicit key-file recovery still needs a trusted root setup with a prepared key path. The guide does not claim physical display/keyboard coverage on every Pi case, exhaustive application coverage, or validation on a second Pi.
