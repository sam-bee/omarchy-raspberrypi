# Pi 5 stability investigation — 24 September 2026

The controlled reboot recovered encrypted NVMe boot, SSH, sudo and the persistent session services, but initially failed desktop acceptance: Quickshell crashed six times during Qt loading. The failure was traced to a difference between buffered and direct reads of one Qt library. Evicting only that file's cached pages restored its expected contents and the visible shell without rewriting the file or changing packages. The underlying cause of the cache discrepancy remains unresolved; this is not a stability acceptance pass.

## Checks and recovery

- Before reboot, ten independent SSH authentications/command sessions with sudo and ten transient systemd services using the production `User` and `PAMName=login` passed. The normal `pam_lastlog2` hook was enabled, and a read-only SQLite integrity check passed.
- SHA-256 checks of 2,077 installed files in `sqlite`, `util-linux`, `util-linux-libs`, `pam`, `openssh` and `glslang` matched their local pacman mtree manifests. This verifies consistency with installed metadata, not an independent signed-archive check.
- One controlled reboot used a one-time NVMe-first override. Persistent EEPROM remained USB-first. A temporary ten-minute systemd fallback timer was enabled for the next boot, then removed after fresh NVMe SSH/sudo access returned; no fallback units remain.
- After boot, both encrypted root and NVMe `/boot` were present. SSH and RDP services were active and no system unit was failed, despite the shell crashes. Service activity alone was therefore insufficient for desktop acceptance.
- `pacman -Qkk qt6-declarative` identified a SHA-256 mismatch in `/usr/lib/libQt6QuickTemplates2.so.6.11.2` (`qt6-declarative 6.11.2-2`). A full check covered 347 installed packages and found no other checksum mismatch. Other differences were boot-file permissions/timestamps and two directory group IDs; that broader check was not an all-clear result.
- With the graphical session stopped, three buffered reads of the Qt library produced `b63008813f701fb5014800dbb80b285129b6c612ad31cfa2628df4f610257a03`. Three `O_DIRECT` reads produced `02a55202f100de1b27abbd12a55dd212191bd0c9f58255fcf30abd3736563614`, matching the installed package manifest. Both byte streams and representative core files were preserved privately off-device.
- After confirming that no process mapped that library, `POSIX_FADV_DONTNEED` was applied to that file alone through a read-only descriptor. A fresh buffered read then matched the expected hash, and all 5,512 qt6-declarative package entries passed `pacman -Qkk`. No package file was replaced and no system-wide cache flush was used.
- Restarting the session restored Hyprland, Quickshell and RDP. A compositor screenshot showed the workspace/clock bar; this was not a new RDP-client acceptance test. Ten further fresh SSH/sudo sessions and ten further systemd PAM sessions passed. The lastlog database remained valid and no further core was recorded during those checks.
- The Qt byte streams differed at 8,165 positions within an 8,192-byte interval. The earlier glslang report recorded a similarly sized span. Its preserved original inode, which had been hard-linked before replacement, now returns the expected signed-package-member hash in three buffered and three direct reads. This weakens the earlier interpretation of persistent file damage; it does not prove what storage returned during that incident or establish a shared cause.

The reusable [stability collector](pi-stability-collector.md) was exercised on the recovered Pi. Its final snapshot completed with 29 PASS, 2 WARN, 1 SKIP and no failed queries. The warnings retain the current boot's six startup cores and two kernel messages; SMART was explicitly skipped because the utility is absent. Local fixtures cover query failures, checksum differences, no-result handling, boot-scoped coredump arguments and private output permissions.

## Limits and next diagnosis

The older SSH/systemd SQLite SIGILL cores omit the file-backed page containing the faulting instruction. Disassembling the current library at that address does not reveal the bytes the CPU saw during the failure. The earlier Hyprland SIGBUS is also unresolved. The new cache discrepancy provides a concrete diagnostic direction but does not establish that all three events share a cause.

The current kernel is `6.18.52-1-rpi`, with 4 KiB pages. The NVMe link reports 5.0 GT/s ×1. Temperature snapshots were approximately 22–24°C and firmware throttling flags were zero; these observations do not exclude intermittent hardware or kernel faults. No NVMe SMART utility was installed, so SMART health remains unmeasured. The NVMe host-memory-buffer size notice and the FAT unclean-volume warning remain in the boot log. No filesystem repair was attempted against the mounted boot volume.

An analogous NVMe corruption bug was fixed by [Raspberry Pi Linux PR #7500](https://github.com/raspberrypi/linux/pull/7500), correcting swapped arguments during DMA unmapping. The [Arch Linux ARM recipe for 6.18.52-1](https://github.com/archlinuxarm/PKGBUILDs/blob/master/core/linux-rpi/PKGBUILD) pins `4bb240615790ea5bd939484f4595b6952ac94ef4`; [the NVMe source at that exact commit](https://github.com/raspberrypi/linux/blob/4bb240615790ea5bd939484f4595b6952ac94ef4/drivers/nvme/host/pci.c#L784) already has the corrected argument order. This source inspection is not a binary-level audit of the running kernel, but it rules out simply assuming the known source fix is absent. No kernel or NVMe parameter was changed during this investigation.

If another package-file mismatch occurs, preserve the buffered and direct byte streams before replacing files, restarting the machine or clearing caches. For a known, non-secret package library, the basic read-only comparison is:

```bash
set -o pipefail
sha256sum /path/to/library.so
dd if=/path/to/library.so iflag=direct bs=4096 status=none | sha256sum
```

Check the pipeline exit status: unsupported or misaligned direct I/O is a failed measurement, not a valid comparison. Do not generalize the successful single-file eviction into automatic recovery; it removes useful diagnostic state and cannot fix an unidentified cause. The next controlled experiment should compare buffered/direct hashes around repeatable file loading and record whether the same bytes or different files fail, with a verified recovery route in place.
