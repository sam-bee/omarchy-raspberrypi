# Read-only Pi stability evidence collector

`collect-pi-stability.sh` records a small, private snapshot of boot identity, selected package versions and package-file checks, service-unit states, current-boot crash/storage messages, coredump metadata, selected mounts, and optional Raspberry Pi thermal and NVMe SMART data. It does not install packages, change configuration, reboot, write to a production database, run stress workloads, read process command lines, or open/dump core files.

Run it from an SSH session as root so system logs, package files, and unit managers can be read. The script uses Bash and standard Arch utilities already present on the Pi; it has no Python dependency. Give it a new output directory outside synced folders when possible:

```bash
sudo bash collect-pi-stability.sh /root/pi-stability-before-reboot
```

The destination must not exist. Without an argument, the script creates `/root/pi-stability-<UTC timestamp>-<pid>` when run as root. It applies mode `0700` to the output directory and `0600` to collected files. Keep the directory private: filtered journal messages, unit names, package state, device paths, and coredump metadata can still reveal operational details. Copy only reviewed artifacts to another location.

The collector bounds external commands with `timeout`. A timed-out or failed query appears as `FAIL` in `status.tsv` and makes the script exit 1. Warnings and unavailable optional sources do not make the collector exit nonzero. The status meanings are:

- `PASS`: the query completed and the stated observation was verified.
- `WARN`: the query found evidence that needs review, such as a matching kernel warning, a failed unit, a package file difference, a listed coredump, or a nonzero firmware throttling flag.
- `SKIP`: the source or utility was unavailable, or no matching device/session was discovered.
- `FAIL`: a query that could be attempted did not complete successfully.

`status.tsv` links each check to its artifact. `summary.txt` counts each status. The package checks use `pacman -Q` and `pacman -Qkk` for `sqlite`, `util-linux`, `util-linux-libs`, `pam`, `openssh`, `glslang`, `qt6-base`, `qt6-declarative`, `glibc`, and `quickshell`. `-Qkk` compares installed files against locally stored package metadata, which can include checksums; this is not independent verification against a trusted signed archive. Recognized file or checksum differences are `WARN` even if `pacman -Qkk` exits nonzero. A timeout or unrecognized query failure is `FAIL`. A missing package is reported as `WARN`, with its integrity check marked `SKIP`.

The journal artifact contains at most 200 current-boot kernel warning-or-higher entries matching crash, storage, filesystem, FAT dirty/unclean, throttling, or voltage terms. Likely password, token, secret, API-key, and bearer-token values are redacted, and individual lines are truncated. Coredump collection validates the kernel boot ID, then lists at most 200 metadata entries matching that exact `_BOOT_ID`; it never opens or dumps a core. Mount collection includes only `/`, `/boot`, `/boot/efi`, `/home`, and `/var`, with no mount options. System and discovered user service-unit artifacts contain unit names and state columns only; user managers are enumerated from logind by numeric UID rather than assuming a username. NVMe SMART collection runs only for controllers discovered under `/sys/class/nvme` and filters model and serial fields.

Review the returned evidence alongside the live investigation and compare it with any earlier snapshot. A `PASS` is limited to the exact query named in `status.tsv`; it is not an overall stability certification.

The focused local fixture checks can be run from the repository root with:

```bash
bash test/shell.d/arm64-stability-collector-test.sh
```
