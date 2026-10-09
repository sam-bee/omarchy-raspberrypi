# Raspberry Pi packaged runtime

The packaged installer variant installs the matching downstream `omarchy` and `omarchy-settings` ARM packages. Commands live in `/usr/bin`, runtime files in `/usr/share/omarchy`, and defaults in `/etc/skel`. New users keep configuration in `~/.config` and generated theme state in `~/.local/state/omarchy`. They do not receive a full runtime checkout or the legacy `~/.local/share/omarchy-pi/current` release pointer.

The initial fresh NVMe installation passed native boot, encrypted automatic unlock, package integrity, XDG state, SSH and RDP acceptance for the installed pair `omarchy`/`omarchy-settings` version `4.0.0.alpha-6857`. A subsequent normal packaged update and reboot also passed on kernel `6.18.55`, with pair version `4.0.0.alpha-6865`, the protected configuration fingerprints preserved, `pacman -Qkk` reporting 1792 runtime files and 473 settings files unaltered, and automatic unlock, SSH, the desktop and RDP service working after reboot. These results describe the tested installed target; they do not claim that the final USB image has been natively booted.

## Build and provenance

Build the pair from a clean pinned checkout:

```sh
python3 install/arm64/build-runtime-packages.py build \
  --source-checkout /absolute/source --output /absolute/new-package-bundle
python3 install/arm64/build-runtime-packages.py --check /absolute/new-package-bundle
```

Pass both archives to `build-desktop-payload.py` with repeated `--runtime-package` arguments and pass the bundle's `manifest.json` with `--runtime-package-manifest`. The payload builder installs the pair before running `provision-desktop-root.sh --runtime-layout packaged`. The installer selects that layout from the verified desktop manifest. Existing payloads without the runtime declaration continue to use the legacy path.

The package map excludes Pi boot, kernel, network and authentication configuration. Pi session units and helpers are package-owned under `/usr/lib/systemd` and `/usr/libexec/omarchy-pi`. The installer continues to prepare the Pi-native boot chain, encrypted target and recovery access. A fresh target's generated Wi-Fi profile sets NetworkManager's `powersave=2` property, so the setting follows that profile without binding it to a particular radio or MAC address.

The pair manifest binds the package names, shared version, `aarch64` architecture, package filenames and SHA-256 values to a full source revision and the SHA-256 of its deterministic Git archive. The installed `omarchy` package carries `.omarchy-pi-source-commit` and `.omarchy-pi-packaged.json`; the latter records packaged mode, channel, version and source provenance. Update dispatch also asks pacman which package owns `/usr/share/omarchy/version` and its settings directory, so a stale `OMARCHY_PATH` or arbitrary source checkout cannot select packaged mode.

There is no trusted downstream ARM package feed yet. The pair is therefore built from the fixed downstream source and reviewed provenance. Repository signatures remain required. If the reviewed local archives are unsigned, the durable worker uses a job-local `LocalFileSigLevel = Optional` entry only for those archives; it does not relax repository or host-wide trust, and this workflow creates no signing key or external feed.

## Normal updates

Use the ordinary upstream command as the desktop user:

```sh
omarchy update
omarchy update -y
omarchy update --status
omarchy update --watch
```

The same command supports both Pi layouts. A source-layout installation is selected from its user-owned `OMARCHY_PATH` release and follows the historical source updater: it resolves the fixed downstream `quattro-rpi5` branch to a full commit, prepares a clean source release, reviews migrations and protected state, updates the Arch Linux ARM transaction, and activates the release. It retains `current` and `previous` releases under the user's `~/.local/share/omarchy-pi` data. Existing source-layout rollback instructions remain in [the ARM64 guide](arm64.md).

On a packaged installation, `bin/omarchy-update` detects the package-owned `/usr/share/omarchy` pair and dispatches to its packaged updater. Before any privileged work, the desktop-user preparation phase runs the pinned `update-source.py prepare`, builds the matching pair with `build-runtime-packages.py`, creates the deterministic source archive, and records a candidate manifest. The manifest binds the source revision and archive digest to both package archives and their digests. The root-owned durable worker snapshots that candidate, then validates native package metadata, the exact settings dependency, package markers, source archive/tree identity, migration policy, and the installed pair before applying pacman.

The package worker still downloads and applies the normal Arch Linux ARM transaction, refuses a pending EEPROM package, verifies protected boot state, and records that a reboot is required. It does not reboot the Pi. The worker does not execute the user's fetched builder or mutable checkout as the update mechanism; it validates the root-owned snapshot, and only explicitly allowlisted migration scripts may run under the desktop account. NetworkManager drop-ins under `/etc/NetworkManager/conf.d` are included in the protected snapshot alongside connection profiles, so local radio policy is reported if an update changes or removes it. Until a signed downstream ARM feed exists, this source-build bridge is the supported normal-update path for packaged installs. `omarchy update-available` performs a read-only check against the pinned downstream source revision and compares it with the installed package source marker; it does not build a package candidate.

Every job is durable under `/var/lib/omarchy-pi-updates/<job>/`. Inspect `update.log`, `result.json`, `package-plan.txt`, and the before/after package lists before retrying a failed job. A failed job may already have changed the Arch Linux ARM package transaction.

## Retained pair and downgrade boundary

The image seeds a root-owned package pair under `/usr/share/omarchy-pi/rollback`. After a successful packaged update, the worker publishes the newly installed pair there for the next update's retained boundary. Before pacman runs, it copies the previously installed pair, its manifest and any required signatures into `/var/lib/omarchy-pi-updates/<job>/previous/`; `result.json.rollback_before` and `result.json.rollback_packages` identify that durable restore point. Use that job-specific pair for an older-version restore, only after checking the manifest, archive existence and SHA-256 values.

When a retained pair is the desired restore point, verify the relevant manifest and SHA-256 values and install both matching archives together:

```sh
sudo pacman -U \
  /var/lib/omarchy-pi-updates/<job>/previous/<omarchy-archive-from-manifest> \
  /var/lib/omarchy-pi-updates/<job>/previous/<omarchy-settings-archive-from-manifest>
```

`pacman -U` supports a downgrade when the previous archives carry an older compatible pair version. Use the plain command when the effective local-file signature policy permits the manifest's verified local archives, including the current Pi policy for this reviewed pair. If the update recorded `/var/lib/omarchy-pi-updates/<job>/pacman-local.conf`, it may be reused with `--config` before `-U` when that host policy requires the recorded override; never copy `LocalFileSigLevel = Optional` into `/etc/pacman.conf`. If neither the effective local policy nor a recorded job-local config permits the archives, stop rather than guessing from the package cache. Verify with `pacman -Qkk omarchy omarchy-settings` afterward. A package restore does not reverse migrations, user configuration, other system packages, boot files, encryption state or a reboot. Do not use a single package, an unverified glob or a source-release pointer as a package rollback.

## Bounded USB repair

Runtime package restoration and boot repair are separate operations. From the installer USB, use the bounded `omarchy-pi-recover` controller with the exact target token from `discover`, and for encrypted roots exactly one of the attached key, existing key file or passphrase credentials:

```sh
omarchy-pi-recover discover
omarchy-pi-recover plan --target /dev/mmcblk0 --confirm-target '<token>' --key-file /path/to/existing-unlock-key
omarchy-pi-recover repair --target /dev/mmcblk0 --confirm-target '<token>' --key-file /path/to/existing-unlock-key --confirm-repair
```

The controller maps the repair confirmation to `REPAIR BOOT ONLY`, revalidates the target and credential, and permits only the configured boot/initramfs repair. It refuses ambiguous, mounted, active-swap, installer or key media targets. It does not format storage, change LUKS keyslots, change EEPROM or boot order, or provide full-system rollback. Recovery acceptance remains a separate check from package installation and update acceptance.

Compared with the legacy layout, runtime versions are system-wide and root-owned. A Git revision must be packaged before activation; changing a user's release symlink no longer switches the runtime. Existing user configuration is not automatically refreshed when defaults change.
