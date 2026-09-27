# Unattended encrypted reboot procedure

Use this procedure after a reviewed package, boot, or recovery change to verify that an encrypted ARM system can reboot without an operator entering a disk passphrase or touching the machine. Keep the dated result, boot identifiers, network addresses, device identifiers, package inventories, hashes, and recovery archive details in private evidence outside this repository.

This gate proves only unattended boot for the tested package and boot state. It does not prove persistent graphical login, a usable desktop session, or remote desktop startup.

## Before reboot

1. Preserve the active recovery route and keep an independent SSH connection available. Do not change boot order or remove the physical recovery media.
2. Capture an exact-state baseline in a root-only location. Include package names, versions, install reasons, protected system and boot files, LUKS metadata, the unlock-device state, firmware configuration, enabled units, default target, network configuration, and relevant symlink targets. Keep the baseline archive and its encrypted off-system copy outside the repository.
3. Verify the backup before rebooting: check the archive and manifest, verify the ciphertext checksum, verify the encryption or signature, list the outer and inner archive contents, and confirm the preserved copy is readable only through the intended recovery process.
4. Inspect the account-local encryption tooling created during the backup. An empty agent directory or other generated state must be recorded as a deliberate baseline exception; no private key should be created by the backup step unless that is explicitly intended.
5. Record the current boot identifier and exact package and boot state in private evidence. Do not put those values in a reusable deployment document.

Confirm the expected layout with read-only inspection before rebooting. The encrypted root, separate boot filesystem, unlock device, default target, and network services must match the reviewed design. Stop if there is unexplained drift.

## Reboot and reconnect

Issue a controlled reboot only after the baseline and recovery checks pass. The existing SSH connection should close normally. Reconnect using strict host-key checking and verify that the machine returns without physical interaction or a passphrase prompt.

On the fresh connection, privately record the new boot identifier and uptime. Inspect the boot journal and device state to confirm that the unlock device became available before the encrypted root was opened, the encrypted root and boot filesystem mounted as expected, and the configured network services returned. Do not publish network addresses or device UUIDs in the deployment repository.

## Post-reboot comparison

Compare the fresh state with the pre-reboot baseline:

- package versions, install reasons, and package database integrity;
- protected system and boot file hashes and modes;
- LUKS metadata, unlock-device state, firmware configuration, and symlink targets;
- enabled units, default target, firewall, routes, and required network services;
- absence of unexpected compositor, graphical-session, or remote-desktop processes.

Investigate every unexplained difference before declaring the gate passed. Store the comparison, command output, boot journal excerpt, and recovery verification in the private evidence record.

## Next gate

Keep SSH as the recovery path. Before installing additional build dependencies, resolve the complete package upgrade against fresh metadata in a scratch database. A nonempty full-upgrade preview blocks a narrow package install on a rolling-release system. After any reviewed transaction, create a new exact-state backup and repeat this procedure. Verify graphical startup and remote desktop separately before enabling persistent user services.
