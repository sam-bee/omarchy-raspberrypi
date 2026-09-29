# Installer image builder

`build-installer-image.py` builds one regular Raspberry Pi 5 installer image
from a signed Arch Linux ARM aarch64 rootfs archive. It never accepts a block
device: `--output` is a new image file which must be written to the desktop
and can later be copied to approved removable media by a separate, reviewed
step.

The default invocation is a read-only plan. It checks that all source files
are present, records their SHA-256 values, validates the expected archive and
`hypr-rdp` hashes, and confirms that the work and output paths are fresh. The
plan does not create a work directory or image. Use a new work directory for
each retry; a failed apply run is retained for inspection and is never resumed
in place.

The apply path must run as root on a native aarch64 build host. It verifies
the detached signature and signer, extracts the rootfs, applies the bounded
package transaction, generates the Pi initramfs, stages the installer
services, and assembles the image. It writes source, package, rootfs, and
build manifests with checksums under the work directory.
Image assembly uses the signed `dosfstools` binary staged at
`/usr/bin/mkfs.fat`; the orchestrator passes that path explicitly, so the
build host does not need a host `mkfs.fat` package.

Example plan:

```sh
python3 build-installer-image.py \
  --archive /var/tmp/ArchLinuxARM-aarch64.tar.zst \
  --signature /var/tmp/ArchLinuxARM-aarch64.tar.zst.sig \
  --keyring /var/tmp/archlinuxarm.gpg \
  --signer-fingerprint FULL_40_HEX_FINGERPRINT \
  --archive-sha256 ARCHIVE_SHA256 \
  --hypr-rdp /var/tmp/hypr-rdp \
  --hypr-rdp-sha256 HYPR_RDP_SHA256 \
  --installer-source-revision FULL_INSTALLER_COMMIT_HASH \
  --desktop-payload /var/tmp/omarchy-pi5-desktop.tar.zst \
  --desktop-payload-sha256 DESKTOP_BUNDLE_SHA256 \
  --workdir /var/tmp/omarchy-installer-build-NEW \
  --output /var/tmp/omarchy-installer-NEW.img
```

Add `--apply` to the same command only after reviewing the plan. The script
does not read or copy `/boot/installer-settings.toml`; the image contains
only the non-secret settings example, so credentials must be supplied later
through the documented first-boot path.

## Prepare the desktop user

The generic desktop payload excludes the build's pacman GPG home, including its generated private master key. After copying the root, the disk installer must initialize a fresh target keyring with `pacman-key --init` and `pacman-key --populate archlinux archlinuxarm` in the target before package operations.

After the target user has been created, the release has been staged at
`~/.local/share/omarchy-pi/current`, and the desktop packages are installed,
run this as that user:

```sh
bash "$HOME/.local/share/omarchy-pi/current/install/arm64/setup-desktop-user.sh"
```

The entry point validates the release pointer, sets `OMARCHY_PATH` and the
release, user-bin and mise-shim paths, then runs the existing theme, image,
mise and user-agent setup leaves in order. It performs no host or sudo work
and leaves the default agent unset. The first run needs network access for
the mise bootstrap and its managed tools.


## Disk installer

The image records a content digest of its staged installer runtime, independently of the desktop source and bundle. Supply `--installer-source-revision` when building from an exported checkout to record its full commit hash; runtime hashes are always recorded. Confirmed jobs and installed-target receipts retain these identifiers. Builds run image verification before reporting success.

The desktop bundle is required, copied into the installer root, and checked before any erasure. The image reserves disk-backed space for its unpacked root/source; package archives remain in the compressed bundle. The builder always runs full image verification before reporting success.

`omarchy-pi-install` opens automatically in the installer Foot terminal and is also available over SSH or locally. A confirmed job runs as one system service. Closing the terminal detaches its progress viewer; reopening the command observes the same job. Durable state and redacted logs live on the installer USB under `/var/lib/omarchy-pi/installer`. Power-loss or failed jobs require a fresh confirmation to restart from the beginning.

The installer account can invoke only the fixed argument-free installer control helper through its image-local sudo rule; authorized-key-only SSH therefore works without adding a general sudo grant. Target configuration and encryption secrets are transient input and are not written to job records.

Installation offers a whole-disk FAT32/ext4 layout, with optional LUKS2 and a recovery passphrase. Automatic encrypted boot requires a separate blank key stick, individually identified and confirmed, which becomes non-bootable ext4 media. Existing key media, mounted disks and the running installer are refused as targets. The installer never changes EEPROM boot order. Keep the installer bootable for recovery and remove it when booting the new target.

The fresh user's mise/Node setup needs internet access. The installer uses its existing network for setup and writes chosen network/access preferences only into the new target. Completion gives hostname/user, access methods and SSH fingerprint; use router/DHCP discovery if the target gets a different address. Shut down separately, remove the installer and leave the key stick attached when using automatic unlock.
