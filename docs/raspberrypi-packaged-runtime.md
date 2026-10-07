# Raspberry Pi packaged runtime

The packaged installer variant installs the matching downstream `omarchy`
and `omarchy-settings` ARM packages. Commands live in `/usr/bin`, runtime
files in `/usr/share/omarchy`, and defaults in `/etc/skel`. New users keep
configuration in `~/.config` and generated theme state in
`~/.local/state/omarchy`. They do not receive a full runtime checkout or the
legacy `~/.local/share/omarchy-pi/current` release pointer.

Build the pair from a clean pinned checkout:

```sh
python3 install/arm64/build-runtime-packages.py build \
  --source-checkout /absolute/source --output /absolute/new-package-bundle
python3 install/arm64/build-runtime-packages.py --check /absolute/new-package-bundle
```

Pass both archives to `build-desktop-payload.py` with repeated
`--runtime-package` arguments and pass the bundle's `manifest.json` with
`--runtime-package-manifest`. The payload builder installs the pair before
running `provision-desktop-root.sh --runtime-layout packaged`. The installer
selects that layout from the verified desktop manifest. Existing payloads
without the runtime declaration continue to use the legacy path.

The package map excludes Pi boot, kernel, network and authentication
configuration. Pi session units and helpers are package-owned under
`/usr/lib/systemd` and `/usr/libexec/omarchy-pi`. The installer continues to
prepare the Pi-native boot chain, encrypted target and recovery access.

For packaged installations, normal `omarchy update` prepares the pinned
downstream source as the desktop user, builds the pair, and hands a private
snapshot to the durable root worker. The worker validates the pair and
source provenance before applying the existing package and migration policy.
This source-build bridge is needed until a downstream ARM package feed is
provided. Repository signatures remain required; local downstream archives
use recorded SHA-256 values and source provenance. No new signing key is
created by this workflow.

The image retains its initial pair under `/usr/share/omarchy-pi/rollback`.
Successful updates retain the newly installed pair for the following update,
and each update job records its previous archives. Reinstalling a previous
matching pair can downgrade the runtime. It does not reverse migrations,
user configuration changes or the rest of a system package transaction.

Compared with the legacy layout, runtime versions are system-wide and
root-owned. A Git revision must be packaged before activation; changing a
user's release symlink no longer switches the runtime. Existing user
configuration is not automatically refreshed when defaults change.

Image preparation and file/emulator validation do not establish native Pi
boot, unlock, RDP or update acceptance. Those checks require a fresh hardware
installation. Preparing a candidate does not alter already published images.
