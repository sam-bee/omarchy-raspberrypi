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

These guards are defense against normal accidental entry, not a sandbox or a complete audit of all Omarchy commands. Direct execution of installation leaves or migration files, raw pacman operations, individual maintenance helpers and independently supplied upstream scripts can bypass them. Do not deploy the tree or treat ARM updates as supported on the strength of these checks. Future updates must also retain this downstream branch rather than replacing it with official package-owned files.

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
