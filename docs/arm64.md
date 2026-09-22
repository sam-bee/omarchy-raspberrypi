# ARM64 / Raspberry Pi 5 compatibility layer

This is the first local development milestone: platform detection, a complete package policy for the pinned upstream base manifest, a read-only plan, and refusal of selected unsafe upstream maintenance paths on ARM. There is no apply mode, installer, package build or deployed desktop in this milestone.

## Provenance

The authoritative branch is `quattro-rpi5` in `sam-bee/omarchy-raspberrypi`. Its official Quattro baseline is `947e2fc002d6831c7888b29b5761d59d29e69727` from `omacom/omarchy`, recorded on 22 September 2026. The implementation adds to that history; it does not merge or continue the third-party ARM branch.

The reference examined in earlier project research is `alexisraitano-myffu/omarchy-arm` at `579f15c699dab01e2b3b12e2c4d2503873359be9`, compared with its upstream base `2c247e390e357ae0fee3f8565b0c816adb705e6a`. Its useful ideas are explicit package classification and separation of ARM hardware from x86 setup. Our planner and guards are independently implemented. We do not source, copy or execute its installer. In particular, its retained upstream networking setup is unsuitable for our Pi's networkd connection.

## Local preview

From the repository root, with Bash and Python 3 installed:

```bash
OMARCHY_PATH="$PWD" ./bin/omarchy-install-plan --target rpi5
OMARCHY_PATH="$PWD" ./bin/omarchy-install-plan --target rpi5 --format json
```

The explicit target is an assumption for offline review. The command makes no SSH connection, reads no credentials, queries no package manager and writes no files. It only reads this checkout and prints the plan. `--target arm64` previews generic ARM64 without the Pi-specific Vulkan candidate. Omitting `--target` detects the local architecture and device-tree model: `arm64` normalizes to `aarch64`, a Raspberry Pi 5 selects `rpi5`, and other aarch64 hosts select `arm64`. An x86 host must select an explicit target. `--apply` and other unknown options fail.

JSON schema version 1 records the target and whether it is explicit or detected, pinned baseline, full package decisions, preservation policy, and outstanding blockers. `allowed_system_changes` is always empty and `ready_to_apply` is always false. Exit zero means a valid plan was rendered; it does not mean packages are available or installation is ready. The baseline describes the reviewed source, not a fresh remote lookup or a claim that the working tree has no further edits.

`install/arm64/packages.tsv` must classify every current entry in `install/omarchy-base.packages`, with no duplicates or stale entries. The planner fails on manifest drift so rebases cannot silently admit unreviewed packages. Additional userspace candidates live in `packages-extra.tsv`. See [package policy](arm64-packages.md) for decisions and limitations. No package candidate is a promise that its dependencies or hooks preserve boot and networking.

## Protected substrate

Our target is an already working Arch Linux ARM Raspberry Pi 5 with encrypted NVMe root. We preserve its Pi-native firmware/kernel boot chain, `linux-rpi`, boot configuration, existing initramfs hooks (`sd-encrypt` present, `kms` deliberately absent), LUKS keyslots, USB key-based unlock, rescue USB and EEPROM boot order. The first desktop layer must not replace any of them.

The host's Arch Linux ARM repositories and keyring remain authoritative. Retain `systemd-networkd`, `wpa_supplicant@wld0`, DHCP/Wi-Fi settings, SSH and current firewall state. Do not run the upstream hardware/config/post-install orchestration, copy all of `etc/`, replay `/etc/skel` over the existing home, enable a display manager, or start a full package update as an incidental desktop setup step. Upstream desktop and Hyprland configuration remain unchanged in this milestone.

## Guard boundaries

Selected system setup, update, migration, repository, reset, hibernation and boot-theme entry points reject non-x86 hosts before mutating work. They do not accept a target override: planner simulation must never authorize native system operations. The focused guard tests enumerate the covered commands and prove refusal before mocked privileged commands or file changes. Migration listing remains read-only and available.

These guards are defense against normal accidental entry, not a sandbox or a complete audit of all Omarchy commands. Direct execution of installation leaves or migration files, raw pacman operations, individual maintenance helpers and independently supplied upstream scripts can bypass them. Do not deploy the tree or treat ARM updates as supported on the strength of these checks. Future updates must also retain this downstream branch rather than replacing it with official package-owned files.

## Local validation and next milestone

```bash
bash test/shell.d/arm64-plan-test.sh
bash test/shell.d/arm64-guards-test.sh
./test/cli
```

Tests use local fixtures and command stubs, not the Pi. A [read-only package audit](arm64-package-audit.md) checked the Pi's cached repositories and identified a Nautilus dependency that would trigger its mkinitcpio hook. Before deployment, re-resolve availability and compatible Hyprland/Quickshell versions against fresh target metadata, inspect dependencies and package hooks, and design an allow-listed deployment of commands, shared assets and existing-user session configuration with backups and rollback. Required desktop helpers that are deferred must be resolved or have explicit tested fallbacks. Do not mark migrations completed simply to suppress failures.

A later on-device rehearsal must validate configuration parsing, shell/bar, terminal bindings, portals/audio, continued SSH/Wi-Fi and unattended encrypted reboot. Headless outputs and RDP are separate unimplemented work. No Pi package transaction, configuration write, service change or reboot was performed for this local milestone.
