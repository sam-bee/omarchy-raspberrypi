# Supervised Arch Linux ARM maintenance gate

**Status: plan only. No maintenance update or apply has been performed.** The 23 September 2026 read-only check found no package candidates in a complete upgrade preview against refreshed scratch repository databases. The persistent tty8/RDP startup and post-reboot real-client checks have since passed; see [persistent startup result](../minimal-session/persistent-startup-result.md). A fresh exact-state encrypted recovery baseline also passed independent verification. These results prove the tested session path, not package-update safety. The scratch preview had no candidates, and its repository database signatures were unavailable, so it did not authorize or exercise an update transaction. Recheck repository state and take a new exact-state baseline before any future maintenance transaction. This is not an Omarchy updater or an apply script.

## Starting point and prerequisites

The Pi currently has 347 packages (35 explicit, 312 dependencies). The real FreeRDP acceptance run `20260923T111237Z` proved live view, keyboard input, pointer input, and disconnect/reconnect to the same Hyprland session through an SSH tunnel. The recovery baseline `20260923T113001Z` captured the exact post-RDP state before persistent-session configuration. After it, the dedicated RDP profile and persistent units were staged. The later encrypted baseline `20260923T120330Z` captured that exact state before reboot, decrypted successfully, and passed an independent protected-state comparison. The controlled reboot and postboot client reconnect then passed; evidence is in [persistent startup result](../minimal-session/persistent-startup-result.md). Take another fresh baseline immediately before any maintenance transaction.

Read-only maintenance preflight on 23 September 2026 found `pacman -Dk` clean and confirmed 347 installed packages. The Pi's cached sync databases were stale (alarm 19 September, core 21 September, extra 21 September, aur 21 August); `pacman -Qu` against those cached databases returned no rows but is not a current update check. A workstation-only scratch copy of the Pi's local package database was resolved against the currently available repository databases without writing to the Pi. The scratch full-upgrade preview returned no candidates. The downloaded database timestamps were alarm 19 September, core 22 September, extra 23 September, and aur 21 August. No repository database signature files were available from the configured mirror, and the Pi config allows unsigned sync databases (`DatabaseOptional`); treat this as a resolver preview, not authenticated update approval. The later transaction plan still requires downloading exact package archives and verifying every package signature with the Pi's trusted keyring. No package transaction was available to apply at the time of the check. Recheck immediately before any maintenance because Arch Linux ARM is rolling release and repository state changes. The live Pi's package database, sync databases, packages, and configuration were not modified by this check.

Do not start until the current state has passed all of these checks:

1. The 23 September persistent-session trial has already passed: boot ID changed to `689027ee-0a2a-4f29-a5d3-989ff6786186`; the encrypted NVMe root, NVMe `/boot` partition, Wi-Fi, SSH, expected units and boot state were checked. The baseline `20260923T120330Z` was independently verified. Repeat these checks after any later boot or state change before relying on this prerequisite.
2. Actual FreeRDP clients connected after that reboot. Keyboard and pointer actions were observed, and client two reconnected to the same Hyprland and hypr-rdp processes after client one closed. The first client frames were initially blank until Foot was opened; this is recorded in the result and bounds the visual claim. A `grim` screenshot copied over SSH is not an RDP test.
3. Keep two independent SSH connections open during maintenance. Have an operator available for HDMI/keyboard and the LUKS passphrase or existing rescue USB if boot or networking fails.

If these prerequisites have not passed, stop here. Record their evidence and the exact package state before using this maintenance plan.

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

## Related records

- [ARM compatibility and guard limits](../../arm64.md)
- [First-session backup and pacman-resolution procedure](../first-session/commands.md)
- [Minimal-session transaction and recovery boundary](../minimal-session/package-stage.md)
- [Current deployment handover](../../../../docs/implementation/arm64-first-layer.md)
