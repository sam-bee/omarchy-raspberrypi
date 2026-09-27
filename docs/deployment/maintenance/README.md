# Supervised Arch Linux ARM maintenance gate

**Status: NVMe maintenance remains a plan.** A disposable-media upgrade and restore rehearsal can provide useful evidence, but it does not authorize an encrypted-NVMe transaction, a complete Omarchy updater, or a general hardware claim. Record machine-specific results, package versions, boot identifiers and recovery details in private deployment evidence. Recheck repository state and take a new exact-state baseline before any future maintenance transaction. This is not an Omarchy updater or an apply script.

## Starting point and prerequisites

Before any maintenance transaction, record the installed package count and reasons, a fresh recovery baseline, and the exact state of boot, storage, network, SSH, session and RDP services. If persistent-session or real-client checks are prerequisites, record their evidence separately and repeat them after any later boot or state change. Take another fresh baseline immediately before any maintenance transaction.

Run a read-only preflight against the target's current package database and boot state. Cached repository databases are not a current update check; resolve a complete upgrade in a scratch copy against freshly retrieved metadata, and treat unsigned or unavailable repository signatures as a failed approval gate. The transaction plan still requires downloading exact package archives and verifying every package signature with the target's trusted keyring. Do not modify the live package database, sync databases, packages or configuration during the preflight. Recheck immediately before any maintenance because Arch Linux ARM is a rolling release.

Do not start until the current state has passed all of these checks:

1. A persistent-session trial has passed for the target system: verify a changed boot identity, the expected root and `/boot` devices, network, SSH, enabled/active units and protected boot state. Repeat these checks after any later boot or state change before relying on this prerequisite.
2. Actual FreeRDP clients connected after that reboot. Keyboard and pointer actions were observed, and client two reconnected to the same Hyprland and hypr-rdp processes after client one closed. The first client frames were initially blank until Foot was opened; this is recorded in the result and bounds the visual claim. A `grim` screenshot copied over SSH is not an RDP test.
3. Keep two independent SSH connections open during maintenance. Have an operator available for HDMI/keyboard and the LUKS passphrase or existing rescue USB if boot or networking fails.

If these prerequisites have not passed, stop here. Record their evidence and the exact package state before using this maintenance plan.

## Boot configuration preservation

Treat the operator's selected Raspberry Pi 5 external PCIe generation in `/boot/config.txt` as protected configuration during kernel, firmware, and desktop maintenance. Before accepting or merging a package-provided `.pacnew`, compare it with the live file and preserve the chosen `dtparam=pciex1_gen` value and its effective conditional scope. Do not require a `[pi5]` section when the accepted live config uses `[all]`; preserve the operator's working configuration. Do not replace the live config or reset the speed automatically.

Before a planned reboot, verify the selected stanza remains present. After reboot, verify the negotiated speed on the actual PCIe storage/device, not just the config text. The new-base preparation default is Gen1; Gen2 is an explicit opt-in because it has been associated with read corruption, NVMe I/O stalls, and boot/desktop failures on a tested system. This system-specific evidence is not proof of a universal Pi 5 hardware defect, and Gen2 stability is not guaranteed. If the selected Gen2 mode causes problems, return to Gen1 and reboot with recovery available. Do not change EEPROM or boot order, and do not offer Gen3 as an option.

## Prepare the reviewed transaction

1. On the workstation, review the downstream branch against a freshly fetched `omarchy-upstream/quattro` and inspect any proposed rebase separately. Run the local ARM planner and guard tests for source changes. This is source review only; do not deploy upstream files or run an Omarchy update command on the Pi.
2. On the Pi, take a fresh private recovery baseline for this exact update. Record package versions and install reasons; pacman configuration, local and sync databases, and log; protected file hashes, symlinks and absent paths; `/boot` inventory; LUKS metadata and header; USB key hash; EEPROM; default target and enabled/active units; routes, addresses and firewall state. Preserve relevant user/session and RDP configuration as well.
3. Encrypt the recovery bundle, copy it outside the repository and synced workspace, and verify that it can be decrypted and listed. The bundle is sensitive: it contains system configuration, key material and a LUKS header. Do not print or publish its contents.
4. Retain signed previous-version archives and detached signatures for every package that the candidate transaction would upgrade. Do not rely on the live pacman cache: Omarchy's update path may prune it. The existing bundle is selective recovery evidence, not a full filesystem image or an atomic rollback point.
5. Resolve against fresh Arch Linux ARM metadata in a scratch copy of the Pi's local pacman database and trusted keyring. Refresh only the scratch sync databases; do not refresh the live database. Preview the complete system upgrade. Arch Linux ARM is rolling release: do not turn this into a partial named-package upgrade. If the complete update is too broad to review, defer it.
6. Download the exact complete candidate set into scratch storage. Verify archive names, versions, hashes and signatures with the existing trusted keyring. Review package metadata, install scripts, archive paths, and both install and removal hook triggers against the Pi's installed hooks. Review sysusers, tmpfiles, udev rules, service presets, and expected account or configuration changes. Run a local-archive transaction preview against the actual installed database and require an exact match with the reviewed set.

Stop and revise the review if a signature or dependency fails, the proposed set drifts, or it includes unexpected removals, replacements, or changes to the Pi boot chain, kernel/firmware, initramfs, storage/encryption, network, SSH, or firewall. Do not omit a protected update while applying the rest of a rolling system upgrade; that would leave a partial upgrade. Keep the current ALARM repositories, pacman configuration, keyring, `linux-rpi`, EEPROM, Pi firmware, `sd-encrypt` hook, and deliberately absent `kms` hook intact.

## Apply and verify once

1. Recheck the live prestate against the backup, confirm there is no pacman transaction in progress, and keep both SSH connections active. Stop if any package, version, service, mount, route, or protected-file state has drifted.
2. In an interactive terminal, install only the exact reviewed local archives in one pacman transaction. The final prompt must match every approved package and version, with no unreviewed removal or replacement. Keep normal dependency and signature checks enabled. Do not use `omarchy update`, `omarchy-refresh-pacman`, `omarchy-channel-set`, `-Syyuu`, `--noconfirm`, `--nodeps`, `--overwrite`, signature bypass, or hook suppression.
3. Before reboot, preserve the pacman log and compare actual package versions and reasons with the manifest. Review all expected hook side effects. Require protected hashes and paths, `/boot`, LUKS/USB/EEPROM state, enabled units, firewall, routes, Wi-Fi, SSH, and the default target to match the new baseline apart from explicitly reviewed effects. Confirm a fresh independent SSH login. Any unexplained difference is a stop condition; do not reboot.
4. If every pre-reboot check passes, perform one controlled reboot with the USB unlock key inserted and the recovery operator available. Reconnect and require a changed boot ID, encrypted NVMe root, `/boot`, expected kernel and boot files, Wi-Fi/address/route, active SSH and networkd/WPA services, and the same protected-state checks.
5. Reconnect with the RDP client and verify the session, keyboard, pointer, and a disconnect/reconnect cycle again. Record the package delta, reboot and RDP results, logs, and any reviewed exceptions beside the new baseline.

## Recovery boundary

For a failed or interrupted transaction, first preserve its log and inspect the actual installed package set; never retry blindly. Roll back only identified packages using their retained, signature-verified previous archives and ordinary pacman dependency checks. Restore only specifically identified files from the recovery bundle after review. Do not copy a saved pacman database over the live database, unpack the entire archive over `/`, rebuild the initramfs as a shortcut, or treat package removal as reversal of generated accounts, caches, or service effects.

If SSH or boot is lost, remote commands cannot recover the machine. Use the available physical console, recorded LUKS passphrase, or existing rescue USB to inspect and restore the affected state. Do not repartition or format either device, rewrite the rescue USB, or change LUKS keyslots as routine rollback.

The current ARM compatibility layer has no package apply mode. Its guards protect selected Omarchy entrypoints against ordinary accidental use; they are not a sandbox for raw pacman commands or individual helper scripts. A future automated updater needs a separate reviewed design and fail-closed tests before it can replace these manual gates.

For a bounded, private, read-only snapshot of Pi stability evidence, use the [Pi stability collector](pi-stability-collector.md). It is a diagnostic aid and does not authorize a maintenance transaction.

For a separate test OS reading an offline filesystem, the [buffered/direct file comparison](file-read-comparison.md) checks a fixed signed-package manifest and preserves the first mismatch. It does not repair files or establish that an intermittent fault is resolved.

The [bounded file-cache refill comparison](file-read-refills.md) adds three file-specific refill cycles, verifies page residency, and requires an off-machine baseline checkpoint before discarding any target file's cached pages.

For the separate before/after desktop-start experiment, the [live-root checkpoint](live-file-read-checkpoint.md) reads a three-file corpus without requesting cache eviction. It records the expected boot and root identity and preserves the first discrepancy.

## Related records

- [ARM compatibility and guard limits](../../arm64.md)
- [First-session backup and pacman-resolution procedure](../first-session/commands.md)
- [Minimal-session transaction and recovery boundary](../minimal-session/package-stage.md)
- [Current deployment handover](../../../../docs/implementation/arm64-first-layer.md)
