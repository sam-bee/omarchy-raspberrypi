# Package resolution record

The machine-specific package resolution, archive review and transaction evidence are retained in the private deployment record. This public note defines the required gate for a new run.

Resolve package metadata in an isolated copy of the target database and trusted keyring. Record the complete closure, repositories, architectures, archive basenames, detached signatures, SHA-256 values and install reasons privately. Verify every archive against fresh metadata and inspect its file list, install scripts, hooks, sysusers/tmpfiles declarations and service files.

The final local transaction must match the reviewed closure exactly, apart from separately approved existing-package changes. Recheck installed package names, versions and reasons after installation, then repeat protected boot, encryption, storage, network, SSH, firewall, account and service checks. Any metadata drift or unexpected state is a stop condition requiring a new private review.
