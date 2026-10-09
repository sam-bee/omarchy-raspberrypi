# ARM64 / Raspberry Pi 5 compatibility layer

This is the ARM64/Raspberry Pi 5 compatibility guide for the `quattro-rpi5` branch. It covers the supported upstream desktop services and controls plus Pi theme-switching compatibility. The reduced session remains a porting mechanism toward the normal upstream Omarchy experience, not a separate desktop product. Deployment evidence belongs in private records; this guide retains the reusable policy and validation boundaries.

## Provenance

The branch should be based on the canonical Omarchy Quattro history and keep its downstream compatibility changes reviewable. Record the exact upstream baseline and tested source revision in private build evidence; do not publish one-machine release identifiers in this guide.

A third-party ARM branch was consulted only as historical research for package classification and separation of ARM hardware from x86 setup. The planner and guards are independently implemented; do not source, copy or execute an external installer. Review network ownership separately instead of assuming that another branch's setup matches the target.

## Local preview

From the repository root, with Bash and Python 3 installed:

```bash
OMARCHY_PATH="$PWD" ./bin/omarchy-install-plan --target rpi5
OMARCHY_PATH="$PWD" ./bin/omarchy-install-plan --target rpi5 --format json
OMARCHY_PATH="$PWD" ./bin/omarchy-install-plan --target rpi5 --pcie-gen 2 --format json
```

The explicit target is an assumption for offline review. The command makes no SSH connection, reads no credentials, queries no package manager and writes no files. It only reads this checkout and prints the plan. A Pi 5 plan defaults to external PCIe Gen1; `--pcie-gen 1|2` makes the selection explicit, with Gen2 carrying a system-specific warning. This option is Pi 5-only, and unsupported generations are rejected. `--target arm64` previews generic ARM64 without the Pi-specific Vulkan candidate or boot policy. Omitting `--target` detects the local architecture and device-tree model: `arm64` normalizes to `aarch64`, a Raspberry Pi 5 selects `rpi5`, and other aarch64 hosts select `arm64`. An x86 host must select an explicit target. `--apply` and other unknown options fail.

JSON schema version 2 records the target and whether it is explicit or detected, pinned baseline, Pi 5 boot policy when applicable, full package decisions, preservation policy, and outstanding blockers. The boot policy proposes a `[pi5]` `dtparam=pciex1_gen=1` stanza for new base preparation and describes explicit Gen2 opt-in, post-reboot speed verification, and rollback. The planner has no installer apply mode and does not change boot files. `allowed_system_changes` is always empty and `ready_to_apply` is always false. Exit zero means a valid plan was rendered; it does not mean packages are available or installation is ready. The baseline describes the reviewed source, not a fresh remote lookup or a claim that the working tree has no further edits.

`install/arm64/packages.tsv` must classify every current entry in `install/omarchy-base.packages`, with no duplicates or stale entries. The planner fails on manifest drift so rebases cannot silently admit unreviewed packages. Additional userspace candidates live in `packages-extra.tsv`. See [package policy](arm64-packages.md) for decisions and limitations. No package candidate is a promise that its dependencies or hooks preserve boot and networking.

## Protected substrate

The target is an already working Arch Linux ARM Raspberry Pi 5. Where the target uses encrypted storage, preserve its Pi-native firmware/kernel boot chain, `linux-rpi`, boot configuration, initramfs hooks, LUKS keyslots, recovery media and EEPROM boot order. The first desktop layer must not replace protected boot or recovery state.

The proposed PCIe Gen1 default applies only while preparing a new Pi 5 base. Existing desktop deployments and updates must preserve the operator's selected `pciex1_gen` value, including when reviewing package-provided `.pacnew` files; they must not silently reset it to Gen2 or Gen1. See the [base preparation policy](deployment/pi5-arch-base.md#raspberry-pi-5-pcie-speed-policy) and [maintenance gate](deployment/maintenance/README.md#boot-configuration-preservation).

The host's Arch Linux ARM repositories and keyring remain authoritative. Preserve the Pi-native boot chain, encrypted root, SSH and firewall state. Network ownership is a separate controlled migration: it requires a fresh recovery baseline, a private root-owned generated profile, an independent SSH check, a bounded rollback timer and confirmation before removing the previous owner. Keep generated credentials and host addresses in private evidence and migration scripts; public documentation must not publish them. Do not run the upstream hardware/config/post-install orchestration, copy all of `etc/`, replay `/etc/skel` over the existing home, enable a display manager, or start a full package update as an incidental desktop setup step.

## Guard boundaries

Selected system setup, update, migration, repository, reset, hibernation and boot-theme entry points reject non-x86 hosts before mutating work. They do not accept a target override: planner simulation must never authorize native system operations. The focused guard tests enumerate the covered commands and prove refusal before mocked privileged commands or file changes. Migration listing remains read-only and available.

These guards are defense against normal accidental entry, not a sandbox or a complete audit of all Omarchy commands. Direct execution of installation leaves or migration files, raw pacman operations, individual maintenance helpers and independently supplied upstream scripts can bypass them. They do not by themselves validate a deployment. Installed Pi updates use the bounded updater below, with separate package, source, migration and recovery checks. Source-layout installs keep a user-owned downstream release; packaged-layout installs keep the runtime in the matching pacman package pair under `/usr/share/omarchy`. Do not replace either path with an unreviewed upstream checkout or package.

## Pi updates

Use the normal Omarchy entry point as the desktop user for both supported Pi layouts:

```bash
omarchy update
omarchy update -y
omarchy update --status
omarchy update --watch
```

The first command asks for confirmation. `-y` confirms the complete update. The `bin/omarchy-update` dispatcher selects the source path when `OMARCHY_PATH` names a valid user-owned Pi release and selects the packaged path only when `/usr/share/omarchy` has the packaged marker and pacman proves ownership by the matching `omarchy`/`omarchy-settings` pair (or matching `-dev` pair). The updater records a job under PID 1, so closing the terminal does not stop it; `--status` reads the latest persisted result and `--watch` reconnects to its log. A completed job records that a reboot is required but does not reboot the machine. Do not run the command with `sudo`; it obtains the required privileged worker through its own boundary.

### Source-layout installs

The source path is the historical Pi layout: the active release is a user-owned checkout under `~/.local/share/omarchy-pi`, selected by the `current` pointer and marked with its full source commit. The updater resolves the fixed HTTPS downstream repository and `quattro-rpi5` branch to a full commit, prepares a clean checkout in the user's private cache, reviews migrations and protected state, updates the Arch Linux ARM package transaction, and activates the new source release. It keeps `current` and `previous` source releases and preserves edited managed configuration. A source commit SHA identifies the downstream archive; it is not a pacman package signature.

For an older source-layout install, source rollback remains a user-session operation:

```bash
python3 "$HOME/.local/share/omarchy-pi/current/install/arm64/update-source.py" rollback --json
```

This changes the user-owned source pointer and managed session files only. It does not undo Arch Linux ARM package changes, migrations, caches, boot files or a reboot. It is not a packaged-runtime rollback and must not be used as one.

### Packaged-layout installs

The packaged path is the current conventional layout: the matched `omarchy` and `omarchy-settings` packages own `/usr/share/omarchy`, while user configuration remains under `~/.config` and `~/.local/state/omarchy`. The same `omarchy update` command is used for both layouts. Before the privileged worker starts, the desktop-user phase runs the packaged runtime's pinned `update-source.py prepare`, builds the matching package pair with `build-runtime-packages.py`, creates a deterministic Git source archive, and records the source revision, source-archive SHA-256, package filenames and package SHA-256 values in the candidate manifest. The worker takes a root-owned snapshot of that candidate before validating it or applying pacman.

There is no trusted downstream ARM package feed yet. The source-build bridge therefore prepares a candidate from the fixed downstream source as the desktop user; the root worker does not execute the user's fetched builder or mutable checkout as the update mechanism. It validates a root-owned snapshot of the native package metadata, exact `omarchy`/`omarchy-settings` dependency and version pair, packaged provenance markers, source archive/tree match, migration allowlist, and the existing package/signature policy before installing. Only explicitly allowlisted migration scripts may run under the desktop account. Repository signatures remain required. If the reviewed local pair is unsigned, the worker uses a job-local `LocalFileSigLevel = Optional` entry only for those local archives; it does not relax repository or host-wide trust.

The package pair is selected by both marker and ownership: `.omarchy-pi-packaged.json` records the packaged mode and source provenance, `.omarchy-pi-source-commit` records the full source revision, and `pacman -Qo --quiet` must identify the two expected owners. A stale `OMARCHY_PATH` or a source checkout cannot force packaged mode. A changed source revision or archive digest is not silently treated as current even when the package version is unchanged.

The packaged worker still updates the normal Arch Linux ARM transaction, checks migrations and protected boot state, verifies the installed result and records a reboot requirement. It does not reboot the Pi. Inspect `/var/lib/omarchy-pi-updates/<job>/` for `update.log`, `result.json`, `package-plan.txt`, and the before/after package lists before retrying a failed job.

The common package phase downloads and applies the complete Arch Linux ARM transaction through pacman with the target keyring and its normal package signature checks. It refuses the transaction when an EEPROM package is pending. The package pair is supplied separately on the packaged path and is deliberately absent from repository resolution.

Before package application, the updater checks the old and candidate source releases. A new or changed ARM migration must have an exact entry in `install/arm64/migrations.allowlist`, including its SHA-256 and a `run` or `skip` decision. Missing, stale or malformed review data stops the job. A `run` entry executes as the detached desktop user with no interactive terminal or guaranteed sudo prompt/keepalive, so it must be noninteractive and desktop-user compatible. Privileged work requires a separately reviewed updater implementation; an allowlist entry alone does not authorize it. Changes to the downstream `hypr-rdp` or `ttfx` package recipe also stop the job; the current updater has no recipe accept override, so delivery requires a separately reviewed package compatibility change. These checks refuse an ambiguous migration or custom recipe; they do not run a broad automatic Pi migration.

For a packaged job, `/usr/share/omarchy-pi/rollback/manifest.json` describes the currently retained pair for the next update. Before pacman runs, the worker copies the previously installed pair, its manifest and any required signatures into `/var/lib/omarchy-pi-updates/<job>/previous/`; `result.json.rollback_before` and `result.json.rollback_packages` identify that durable restore point. Use that job-specific pair for an older-version restore, only after checking the manifest, archive existence and SHA-256 values:

```bash
sudo pacman -U \
  /var/lib/omarchy-pi-updates/<job>/previous/<omarchy-archive-from-manifest> \
  /var/lib/omarchy-pi-updates/<job>/previous/<omarchy-settings-archive-from-manifest>
```

`pacman -U` can downgrade a package when the previous version is older, but the selected pair must be the same channel and compatible version. Use the plain command when the effective local-file signature policy permits the manifest's verified local archives, including the current Pi policy for this reviewed pair. If the update recorded `/var/lib/omarchy-pi-updates/<job>/pacman-local.conf`, it may be reused with `--config` before `-U` when that host policy requires the recorded override; never copy `LocalFileSigLevel = Optional` into `/etc/pacman.conf`. If neither the effective local policy nor a recorded job-local config permits the archives, stop rather than guessing from the package cache. Verify with `pacman -Qkk omarchy omarchy-settings` afterward. This restores only the two runtime packages. It does not reverse migrations, user configuration, other system packages, boot files, encryption state or a reboot. The installer USB path below provides bounded boot/initramfs repair when that is the actual fault; it is not a full-system rollback.

## USB recovery commands

From the installer USB, run `omarchy-pi-recover` as the installer session user. It submits requests through the fixed root controller; credentials are read transiently and are not written to job records. For an attached unlock-key USB, use the partition and exact key token returned by `discover` with `--key-device` and `--confirm-key`. The controller mounts that selected key temporarily read-only during inspection or repair, then cleans it up. Existing `--key-file` and `--passphrase` options remain available.

Start with the read-only inventory and save the exact target token printed for the intended disk:

```bash
omarchy-pi-recover discover
omarchy-pi-recover status
omarchy-pi-recover status --watch
```

Copy the exact target token returned by `discover` into `--confirm-target`; do not reconstruct or shorten it. The same response lists eligible attached key partitions with their parent stable ID, filesystem UUID, size/model and exact key token. When more than one key USB is present, match the stable ID and UUID to the physical media before selecting its partition path. For an encrypted root, planning, inspection and repair require exactly one credential: an attached key using `--key-device` plus `--confirm-key`, an existing `--key-file`, or `--passphrase`. For a plaintext root, omit all credential flags.

```bash
omarchy-pi-recover plan \
  --target /dev/mmcblk0 \
  --confirm-target '<token from discover>' \
  --passphrase

omarchy-pi-recover inspect \
  --target /dev/mmcblk0 \
  --confirm-target '<token from discover>' \
  --key-file /path/to/existing-unlock-key

omarchy-pi-recover plan \
  --target /dev/mmcblk0 \
  --confirm-target '<target token from discover>' \
  --key-device /dev/<key-partition> \
  --confirm-key '<key token from discover>'

omarchy-pi-recover inspect \
  --target /dev/mmcblk0 \
  --confirm-target '<target token from discover>' \
  --key-device /dev/<key-partition> \
  --confirm-key '<key token from discover>'
```

`plan` returns a bounded unlock/repair plan without unlocking or mounting anything; with `--key-device`, it revalidates the key identity and token without opening the key filesystem. `inspect` unlocks an encrypted volume read-only, mounts the selected key with restrictive read-only options, mounts the root with `ro,noload` and the boot partition read-only, then cleans up all mounts. `repair` uses the same credential choices and requires the same exact target token plus `--confirm-repair`:

```bash
omarchy-pi-recover repair \
  --target /dev/mmcblk0 \
  --confirm-target '<token from discover>' \
  --key-file /path/to/existing-unlock-key \
  --confirm-repair

omarchy-pi-recover repair \
  --target /dev/mmcblk0 \
  --confirm-target '<target token from discover>' \
  --key-device /dev/<key-partition> \
  --confirm-key '<key token from discover>' \
  --confirm-repair
```

The controller maps `--confirm-repair` to the exact internal confirmation `REPAIR BOOT ONLY` and then permits only the configured boot and initramfs repair. It refuses ambiguous, mounted, active-swap, installer or key media targets and does not format storage, change LUKS keyslots, change EEPROM or boot order, or provide full-system rollback. Native Pi acceptance of this recovery flow remains a separate test gate.

`omarchy-pi-recover` intentionally exposes only `discover`, `plan`, `inspect`, `repair` and `status`. Run `omarchy-pi-install` for the guided Install or Repair home.

The new upstream lock PAM file is an explicit operator-approved change; staging does not apply it automatically. Retain the recorded authorization and rollback baseline; existing authorization for this exact change does not need to be requested again. Staging must preserve existing authentication policies.

## Current upstream desktop status

The Pi profile should enable the supported upstream window-management bindings, lock, idle, media, nightlight and polkit services wherever the hardware and package set support them. Battery controls may remain disabled on hardware without a battery, and suspend requires a separately validated hardware interface. Validate temporary windows, secure lock/unlock, polkit authorization, nightlight, media controls and theme selection. Treat physical keyboard/pointer input and physical display color as separate acceptance gates.

Theme switching should follow the normal picker and hooks. Verify that the selected theme, background and terminal palette survive a reload and a controlled restart. If browser-policy integration or an icon theme is unavailable, use the documented fallback and record the package and helper state in private evidence.

Network ownership is a controlled migration. Require a fresh recovery baseline, a private root-owned generated profile, an independent SSH check, a bounded rollback timer and confirmation before removing the previous owner. Keep generated credentials, host addresses and scan results in private evidence. A read-only network-panel scan does not establish that connecting to new networks or toggling the radio is safe.

The pinned `ttfx` ARM recipe is at [`install/arm64/packages/ttfx/PKGBUILD`](../install/arm64/packages/ttfx/PKGBUILD). Build and install it only through a reviewed target-local transaction, then validate the renderer, idle/screensaver path and existing-password unlock. Keep the package policy separate from the desktop source stage. Record package versions, renderer output and protected-state checks in private evidence.

A controlled reboot and persistence check is a required acceptance gate. Verify the selected kernel and modules, boot order, PCIe policy, storage/encryption state where configured, network, SSH, desktop, audio, clipboard and agent paths, protected files and service health. A passing reboot establishes only the tested target and workload; it does not establish sustained stress, universal hardware behavior or physical-display acceptance.

## Remaining gates

Before installer distribution, replay the deployment from a clean installation and compare protected boot, encryption, SSH and network state. Verify physical keyboard/pointer input and physical display color separately. Preserve boot order, PCIe policy, credentials and recovery media throughout the checks.

## Local validation and next milestone

The [first-session deployment plan](deployment/first-session/README.md) defines a four-root compositor/terminal smoke, exact session files, backups, transaction gates and rollback. Resolve the complete package transaction against target-local metadata, verify the required signatures and hooks, and retain machine-specific package, boot and session results in private evidence. The smoke does not establish full Quattro startup, reboot persistence, physical display or universal hardware support.

```bash
bash test/shell.d/arm64-plan-test.sh
bash test/shell.d/arm64-guards-test.sh
./test/cli
```

These planner and guard tests use local fixtures and command stubs. Resolve every package candidate and dependency against fresh target-local metadata, review initramfs and other hooks, and require backups and rollback before any configuration change. Required desktop helpers that are deferred must be resolved or have explicit tested fallbacks. Do not mark migrations completed simply to suppress failures.

The planner milestone performs no target transaction, configuration write, service change or reboot. Local smoke tests validate command and fixture behavior; target acceptance must separately verify the renderer, session, desktop controls, theme, idle/screensaver, network panel, reboot persistence, physical input and physical display. A virtual `hypr-rdp` output does not establish physical display or real-client acceptance.
