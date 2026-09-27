# Minimal UWSM and Quickshell package stage

This runbook installs the package closure needed by the minimal Pi session. It is a procedure, not a frozen package manifest. Resolve current metadata for the target architecture and retain the exact names, versions, hashes, signatures and install reasons in the private deployment record.

The earlier [first-session smoke procedure](../first-session/README.md) must have passed its preservation gate before this stage. Keep two independent recovery connections and an encrypted, test-decryptable baseline throughout.

## Package gate

Resolve the closure using a copy of the target's package database and trusted keyring. Do not refresh the live database as an incidental probe or use a partial upgrade. Download the complete approved closure before installation, verify detached signatures and hashes, and inspect archive paths, install scripts, hooks, sysusers/tmpfiles declarations and service files.

The closure normally includes the UWSM compositor-session tooling, Quickshell, terminal selection, file-watch support, Wayland support and a frame-capture utility. The exact package roots and dependencies are target and repository state; record them privately and review any provider, replacement, upgrade or removal.

Reject any archive that touches the kernel, firmware, bootloader, initramfs, encryption, storage, SSH, network or firewall substrate unless that change has a separate review. Do not use `--nodeps`, `--overwrite`, signature bypasses or hook suppression.

## Transaction and preservation checks

Before the transaction, capture package names and install reasons, protected file hashes and symlinks, account files, boot and encryption metadata, mounts, enabled units, default target, firewall rules, addresses, routes and active SSH/network services. Keep the recovery bundle private.

Use one interactive local-archive transaction. The prompt must contain exactly the reviewed additions and any separately reviewed existing-package transition. Mark only the intended package roots explicit after success. Run the package database consistency check and retain the transaction log.

Package hooks may reload managers, create declared system identities, regenerate caches or install disabled units. Review those effects privately. Installing a unit does not authorize enabling or starting it, and package removal may not remove a sysusers-created identity or generated cache.

After the transaction, compare the actual package set and install reasons with the private manifest. Repeat the protected-state checks and make a new independent SSH connection. Verify that unexpected units remain disabled and inactive, the foreground VT is unchanged and no new boot or encryption path appeared. Stop before session staging if any check differs.

## Recovery boundary

If the transaction is interrupted, inventory the actual package state before retrying. Roll back only the reviewed additions with an interactive removal prompt and without recursive orphan deletion. Leave generated identities and caches for a separately reviewed cleanup. Do not copy a saved package database over live state, unpack a complete system archive, rebuild the initramfs or reboot as routine rollback.

Once the package gate passes, continue with [the bounded session runbook](session-stage.md). Keep the package manifest and acceptance result in the private evidence tree for the particular machine.
