# Preparing an Arch Linux ARM base for Raspberry Pi 5

This is a working procedure for producing a bootable Arch Linux ARM base on **designated blank or disposable media**. It is the step before the [minimal Omarchy session deployment](pi5-minimal-install.md). It does not install Omarchy, configure disk encryption, or prepare a finished installer image.

The procedure below records a successful 23 September 2026 rehearsal on one Raspberry Pi 5 (8 GB) and one USB drive. It is not yet an independently reproduced or hardware-certified installer. Arch Linux ARM's current download catalog labels its signed Raspberry Pi aarch64 root filesystem for Pi 3/4; its downloads page says systems without a specific image must supply their own kernel. The rehearsal used that aarch64 root filesystem, then replaced its generic kernel/boot path with Arch Linux ARM's `linux-rpi` and `raspberrypi-bootloader` packages. Check the current catalog and package names before repeating this rolling-release procedure. [Arch Linux ARM downloads](https://archlinuxarm.org/about/downloads) · [Raspberry Pi 5 boot configuration](https://www.raspberrypi.com/documentation/computers/config_txt.html).

## Scope and recovery

Use a spare Pi where possible. If testing removable media on a Pi that already has an operating system, preserve its current storage, EEPROM settings, and normal boot order. This procedure assumes the new root and boot partitions are separate from the currently running root. It does not cover repurposing a disk that also contains an existing system, recovery key, or user data. Make and test a full-device backup before choosing any such disk; file copies alone do not preserve partition tables or boot configuration.

Have a local recovery route before first boot: serial console or monitor and keyboard, known-good boot storage, and a second computer that can re-image the designated test drive. Prefer wired Ethernet for the first boot. Do not put personal data on this unencrypted test base.

## Download and verify the root filesystem

Download the aarch64 Raspberry Pi rootfs and detached signature from the official Arch Linux ARM release endpoint. The downloads page lists the signing key ID, and the package-signing page lists its full fingerprint; the signer used in the September 2026 rehearsal was `68B3537F39A313B3E574D06777193F152BDBE6A6` (Arch Linux ARM Build System). Verify the fingerprint through the official site or a separately trusted Arch Linux ARM keyring before accepting it. A locally calculated SHA-256 is useful to identify and reproduce the exact archive, but it does not replace signature verification.

For example, on a trusted Arch Linux ARM machine with its initialized keyring:

```bash
set -euo pipefail
work=/var/tmp/archlinuxarm-pi5
install -d -m 0700 "$work"
archive_url=http://os.archlinuxarm.org/os/ArchLinuxARM-rpi-aarch64-latest.tar.gz
curl --fail --location "$archive_url" --output "$work/rootfs.tar.gz"
curl --fail --location "$archive_url.sig" --output "$work/rootfs.tar.gz.sig"
gpgv --keyring /etc/pacman.d/gnupg/pubring.gpg \
  "$work/rootfs.tar.gz.sig" "$work/rootfs.tar.gz"
sha256sum "$work/rootfs.tar.gz" | tee "$work/rootfs.sha256"
```

The official release table currently links to this endpoint over HTTP, so the detached signature supplies the archive authenticity check. Do not skip signature verification or disable TLS checks when choosing another mirror. Require a good detached-signature result from the expected full fingerprint. Stop if the signature is missing, invalid, or signed by a different key. Keep the URL, retrieval date, archive size, SHA-256, signature result, and fingerprint in the build record. The official [downloads page](https://archlinuxarm.org/about/downloads) says releases are signed with the package-signing key; the [package-signing page](https://archlinuxarm.org/about/package-signing) lists the build-system key.

## Identify and prepare the test media

First inventory devices without writing to them:

```bash
lsblk -o PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,FSTYPE,MOUNTPOINTS
ls -l /dev/disk/by-id/
```

Select the whole-disk `/dev/disk/by-id/…` path for the disposable drive, not a `/dev/sdX` name and not a partition. Record the exact model, serial, and byte capacity from the output. The following example intentionally stops until those values and the exact path are filled from the operator's own inventory. It verifies identity, checks that no partition is mounted and that the disk does not back the running root, prints the target again, and requires typing the full path before creating filesystems. Run it as root, and stop if any check differs from the drive you designated.

```bash
set -euo pipefail
TARGET=/dev/disk/by-id/REPLACE_WITH_THE_DISPOSABLE_DISK_ID
EXPECTED_MODEL='REPLACE WITH THE OBSERVED MODEL'
EXPECTED_SERIAL=REPLACE_WITH_THE_OBSERVED_SERIAL
EXPECTED_BYTES=REPLACE_WITH_THE_OBSERVED_BYTE_CAPACITY

[[ "$TARGET" == /dev/disk/by-id/* && -b "$TARGET" ]]
[[ "$(lsblk -dn -o TYPE "$TARGET" | xargs)" == disk ]]
[[ "$(lsblk -dn -o MODEL "$TARGET" | xargs)" == "$EXPECTED_MODEL" ]]
[[ "$(lsblk -dn -o SERIAL "$TARGET" | xargs)" == "$EXPECTED_SERIAL" ]]
[[ "$(blockdev --getsize64 "$TARGET")" == "$EXPECTED_BYTES" ]]
if lsblk -nrpo MOUNTPOINT "$TARGET" | grep -q '[^[:space:]]'; then
  echo 'Stop: a partition on this disk is mounted.' >&2
  exit 1
fi
root_source=$(findmnt -n -o SOURCE /)
if lsblk -s -nrpo PATH "$root_source" | grep -Fxq "$(readlink -f "$TARGET")"; then
  echo 'Stop: this disk backs the running root filesystem.' >&2
  exit 1
fi
lsblk -o PATH,TYPE,SIZE,MODEL,SERIAL,FSTYPE,MOUNTPOINTS "$TARGET"
read -r -p 'Type the exact disposable disk path to erase it: ' confirmation
[[ "$confirmation" == "$TARGET" ]]

BOOT_PART="${TARGET}-part1"
ROOT_PART="${TARGET}-part2"
parted --script "$TARGET" \
  mklabel msdos \
  mkpart primary fat32 1MiB 513MiB \
  mkpart primary ext4 513MiB 100% \
  set 1 lba on
udevadm settle
[[ -b "$BOOT_PART" && -b "$ROOT_PART" ]]
mkfs.fat -F 32 -n PI-BOOT "$BOOT_PART"
mkfs.ext4 -F -L PI-ROOT "$ROOT_PART"
blkid "$BOOT_PART" "$ROOT_PART"
```

This creates a 512 MiB FAT32 boot partition and an ext4 root partition using an MBR partition table. It irreversibly replaces the selected disk's partition table and filesystems. Check `lsblk` and `blkid` again after formatting; record the generated filesystem UUIDs. The partition path suffix `-partN` is provided by udev for the selected by-id disk. If it is absent, stop and resolve the device naming before continuing.

## Extract the root and install the Pi 5 boot packages

Mount the new filesystems and extract the verified archive as root so ownership, permissions, ACLs, and extended attributes are preserved. Make sure the mount directories are new and empty before use; do not extract into a host root filesystem.

```bash
ROOT_MNT=/mnt/archlinuxarm-pi5
[[ ! -e "$ROOT_MNT" ]]
install -d -m 0755 "$ROOT_MNT"
mount "$ROOT_PART" "$ROOT_MNT"
install -d -m 0755 "$ROOT_MNT/boot"
mount "$BOOT_PART" "$ROOT_MNT/boot"
bsdtar -xpf "$work/rootfs.tar.gz" -C "$ROOT_MNT"
```

Use a trusted native aarch64 Arch Linux ARM bootstrap system for the target package transaction. Initialize/use the Arch Linux ARM package keyring, and resolve against the current official Arch Linux ARM repositories. Review the **whole** target transaction and verify all package signatures. Install the Pi-native `linux-rpi` kernel and `raspberrypi-bootloader` package into the target root. The tested rootfs also contained generic kernel/U-Boot packages that conflicted with this boot path; allow only reviewed removals from the target root. Never run a target package transaction against the bootstrap host's root by mistake. Arch Linux ARM documents rootfs extraction with `bsdtar` and keyring initialization in its [generic AArch64 installation instructions](https://archlinuxarm.org/platforms/armv8/generic).

Run package hooks inside the target root (for example, with `systemd-nspawn -D "$ROOT_MNT"`) so initramfs generation reads the target's package database and configuration. Do not copy a kernel or initramfs from a different Pi installation. The tested unencrypted base used a `mkinitcpio` hook set equivalent to `base systemd modconf keyboard sd-vconsole block filesystems fsck`; it included ext4 and USB storage support at root-mount time. Inspect the target kernel configuration and initramfs for the kernel/media combination you actually select. Build the target initramfs after installing the kernel, and require nonempty kernel and initramfs files in the FAT partition.

The tested boot files were `kernel8.img`, `initramfs-linux.img`, `config.txt`, and `cmdline.txt`. Raspberry Pi 5 firmware is in the board's EEPROM. Pi 5 firmware normally tries `kernel_2712.img` first and can fall back to `kernel8.img` when it is absent; set the kernel name explicitly if your selected package and firmware require it. Pi 5 also requires a nonempty `config.txt` on the boot partition. The rehearsal copied a known-good Pi 5 configuration with several default settings; its boot-critical lines included:

```ini
initramfs initramfs-linux.img followkernel
dtoverlay=vc4-kms-v3d-pi5
```

For an unencrypted ext4 root, make the target's `/etc/fstab` refer to the new root and boot UUIDs. For example:

```fstab
UUID=<root-filesystem-uuid>  /      ext4  defaults,noatime  0 1
UUID=<boot-filesystem-uuid>  /boot  vfat  defaults          0 2
```

Set the kernel command line to one line such as:

```text
console=serial0,115200 console=tty1 root=UUID=<root-filesystem-uuid> rw rootwait
```

Use the actual UUIDs reported by `blkid`. Keep the command line on one line. Do not copy another machine's root UUID or encryption arguments. Encrypted roots require a separately designed key enrollment, initramfs, recovery, and boot test; this guide does not configure them.

Before unmounting, verify `config.txt` is nonempty; check that the named kernel, initramfs, DTBs, overlays, and required firmware files exist; confirm `/etc/fstab` and `cmdline.txt` point to the intended target UUIDs; and confirm the root filesystem and boot partition are mounted from the selected disk. Sync and unmount cleanly.

## First boot and administration

The Arch Linux ARM base image may contain default `alarm` and `root` accounts and can start SSH. Keep the new media disconnected from the network until those defaults have been replaced or locked. Attach a local console, boot the test media, create a new administrator with a unique password, install `sudo`, lock or remove the default accounts, and configure SSH to reject root and password login once key-based access has been confirmed. Add only your own public key; protect its `authorized_keys` file and any Wi-Fi configuration as root-owned/private data. Do not place passwords or private keys in this guide, shell history, a repository, or an unencrypted image.

With local administration working, connect Ethernet or configure the intended network and enable the chosen network manager plus `sshd`. Discover the DHCP lease from the router or local console rather than assuming an IP address. Record the new SSH host-key fingerprint from the console and verify it on the client before accepting the first remote login. Then initialize/populate the Arch Linux ARM keyring as documented, refresh package metadata, and apply a complete signed system update before installing the desktop profile. Arch Linux ARM is rolling release; record the repository database date and exact package versions rather than copying versions from this rehearsal.

## Boot test and recovery

For a removable-media rehearsal beside a working Pi installation, preserve the existing EEPROM configuration and normal boot order. Save the bootloader configuration first. Raspberry Pi documents a one-time Pi 5 boot-order override through `vcmailbox` (`set_reboot_order`); use the current official syntax only when needed to select the USB test system, and confirm that the change is one-time. Do not write a persistent boot-order change as part of this guide. See the official [Pi boot-order documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#BOOT_ORDER) and [`set_reboot_order`](https://www.raspberrypi.com/documentation/computers/config_txt.html#set_reboot_order).

After boot, verify from the local console and a fresh independent SSH connection:

```bash
uname -a
cat /proc/cmdline
findmnt -no SOURCE,FSTYPE,OPTIONS /
findmnt -no SOURCE,FSTYPE,OPTIONS /boot
lsblk -o PATH,TYPE,SIZE,FSTYPE,UUID,MOUNTPOINTS
systemctl is-active sshd
pacman -Dk
```

Confirm that `/` and `/boot` come from the intended drive, the kernel and root UUID are expected, networking survives an SSH reconnect, the package database is consistent, and no encrypted or domestic disk has been changed. Test a normal reboot and confirm the Pi returns to its previously selected system when the one-time test boot has expired. If the test system does not boot, remove its media and use the saved boot configuration/known-good system or local console to diagnose it; do not change EEPROM settings blindly. Keep a copy of the verified rootfs archive and build record off the target so the disposable medium can be recreated.

## Evidence and open work

The single-device rehearsal booted the signed rootfs from USB with `linux-rpi` and `raspberrypi-bootloader`, established Wi-Fi and key-based SSH, and verified the ext4 root plus FAT boot mounts. It then installed the minimal desktop package set and launched the headless V3D session described in the [session guide](pi5-minimal-install.md). The domestic NVMe path was separately boot-tested after the temporary media change.

The rehearsal did not establish physical HDMI login, a fresh-device setup without a pre-existing Arch Linux ARM bootstrap host, package-upgrade/rollback acceptance, or reproducibility across different Pi 5 boards and storage controllers. No installer image is published here. Before calling this a general install guide, reproduce from independently downloaded and signature-verified inputs on another Pi 5, record the full target transaction and boot files, and exercise both local recovery and ordinary display login. An image builder should then take stable media identity checks, fresh per-install SSH host keys, account provisioning, and omission of credentials as explicit design requirements.
