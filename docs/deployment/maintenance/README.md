# Supervised Arch Linux ARM maintenance gate

**Status: plan only. No maintenance update or apply has been performed.** Use this runbook for one supervised Arch Linux ARM package update after unattended encrypted boot and an actual RDP client session have both been proved on the current Pi state. It is not an Omarchy updater or an apply script.

## Starting point and prerequisites

The last recorded package state is 329 packages (30 explicit, 299 dependencies) after the minimal UWSM session stage. The encrypted recovery baseline `20260923T012942Z` predates any later RDP or persistent-session work and cannot be used as the pre-update backup. The current package set, pending updates, boot state, and RDP setup have not been checked by this plan; gather them again on the Pi before deciding whether an update is safe.

Do not start until the current state has passed all of these checks:

1. Reboot with the USB unlock key present. Record a changed boot ID and verify the encrypted NVMe root, `/boot`, Wi-Fi, SSH, expected services, and boot files. This proves this boot's unattended unlock; old notes alone do not.
2. Connect with an actual RDP client. Verify the expected session is visible, keyboard and pointer input work, and disconnecting and reconnecting restores access. A `grim` screenshot copied over SSH is not an RDP test.
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

## Related records

- [ARM compatibility and guard limits](../../arm64.md)
- [First-session backup and pacman-resolution procedure](../first-session/commands.md)
- [Minimal-session transaction and recovery boundary](../minimal-session/package-stage.md)
- [Current deployment handover](../../../../docs/implementation/arm64-first-layer.md)
