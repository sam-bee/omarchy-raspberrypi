# Package blocker resolved — 23 September 2026

The missing-package problem is resolved. Arch Linux ARM now publishes `bubblewrap 0.13.0-1`; the Pi's unchanged live sync database still names the unavailable `0.12.0-1`. A current native dependency resolution, verified downloads and an unprivileged Bubblewrap test provide a concrete path to the first graphical test. No package was installed or upgraded during this investigation.

## Revised transaction

The [current manifest](packages-2026-09-23-resolved.tsv) records **107 archives, 145.05 MiB**: the same 106 new desktop packages, with Bubblewrap updated to `0.13.0-1`, plus the only pending installed-package update, `expat 2.8.4-1 → 2.8.5-1`. The four explicit desktop roots are unchanged. All 105 unchanged desktop archives have the same version and SHA-256 as the prior content review.

This was resolved using the Pi's native pacman against a separate copy of its local package database, fresh sync databases, a private copy of its signing keyring and a separate log/cache. The full-upgrade preview contains only Expat. Resolving the full upgrade together with the desktop roots avoids selecting new desktop packages while silently omitting that pending base-library update. The exception to the earlier no-upgrades proposal is exactly this reviewed Expat transition; it does not authorize kernel, firmware, encryption, networking, SSH or other system upgrades.

`pacman -Suw --needed` with the four desktop roots downloaded all 107 archives and completed its keyring/integrity checks under the existing `PackageRequired` / `PackageTrustedOnly` policy. This is **download-only**: pacman's log describes its full-upgrade calculation, but no ALPM install transaction was performed. A separate SHA-256 check matched all 107 files to their metadata, and all detached signature files are retained. The 191 installed package names/versions still exactly match the committed prestate.

A final `pacman -Up` preview of those local archives against the actual installed package database also matched all 107 approved name/version pairs exactly. `pacman -Qp` confirmed that the retained rollback archive identifies itself as `expat 2.8.4-1`.

## Bubblewrap verification

- Official ARM archive: `bubblewrap-0.13.0-1-aarch64.pkg.tar.xz`.
- SHA-256: `4866e25c5a7c33c3ded9f221058b5f4136cdbd991f3dc77a2f7a740368c95a61`.
- Signature: Arch Linux ARM Build System, fingerprint `68B3537F39A313B3E574D06777193F152BDBE6A6`; the copied Pi keyring reports full trust. Luna independently verified the same archive and signature from an official named HTTPS mirror.
- Contents: ordinary mode-0755 `bwrap`, completions and a manual page. No install script, package hook, service, boot file or firmware. Runtime dependencies remain `glibc`, `libcap`, `libgcc`; the legacy `bubblewrap-suid` replacement is irrelevant because that package is absent.
- The Pi reports `CONFIG_USER_NS=y`. The signed `bwrap` executable was extracted only into the scratch directory and run as Sierra. `bwrap --version` returned `bubblewrap 0.13.0`; an isolated sandbox running `/usr/bin/true` exited successfully. It was not installed into `/usr/bin` or added to PATH.

This verifies the package's basic sandbox operation on the actual kernel. It does not claim that Glycin, Hyprland or a graphical session has been runtime-tested.

## Expat recovery and review boundary

The old signed `expat-2.8.4-1-aarch64.pkg.tar.xz` and `.sig` are present in the Pi's package cache and have been copied to the private scratch rollback directory. Its signature verifies with full trust under the same ALARM signing fingerprint. Archive SHA-256: `95b99acc39cb71d84fe2a7de224b0efb2fd1f383f9f6386508c280601e8714ec`. The installed library SONAME is `libexpat.so.1`.

Luna independently reviewed the new ARM archive, SHA-256 `51e735455d69e6d489a0d955955de3c7135cb1837e76fa490c4b19c97d598ee0`. It depends only on `glibc`, retains SONAME `libexpat.so.1`, and contains library/tool/header/documentation files. There is no install script, hook, service, udev rule, sysusers/tmpfiles declaration or recorded initramfs trigger path. The package changelog retains the same ABI interface age across the patch update. This supports a narrow maintenance exception, not a general permission to update protected system components.

The new `xmlwf` and `libexpat.so.1.12.5` were also extracted into the scratch directory and run as Sierra against a small valid XML file. A process-scoped `LD_LIBRARY_PATH` and dynamic-loader trace confirmed the test used the staged new library with the Pi's existing libc; parsing exited successfully. The installed library was not replaced. This is a basic runtime compatibility check, not a complete ABI/security regression test.

The revised runbook retains this archive in the recovery bundle, preserves Expat's original install reason, accepts only the exact reviewed version transition, and restores the signed old package after removing the new desktop packages if rollback is needed. Pacman's normal dependency checks remain enabled. Removing only the newly added packages would leave Expat updated, so that alone would not restore the starting package set.

## Preserved state and retained evidence

After the isolated refresh, download and sandbox test, checks confirmed:

- Installed package names/versions unchanged; live `/var/lib/pacman/sync/*.db` hashes unchanged.
- `/etc/pacman.conf`, `/boot/config.txt`, `/boot/cmdline.txt`, `/boot/initramfs-linux.img` and `/etc/mkinitcpio.conf` hashes unchanged.
- SSH, networkd and `wpa_supplicant@wld0` still active; independent SSH connections succeeded.

Only temporary investigation files were created on the Pi under `/var/tmp/omarchy-pi-resolution.2F1dct`: scratch databases, keyring, logs, verified package/signature cache, old Expat rollback files and the extracted Bubblewrap/Expat test files. This directory is not a recovery backup and may be cleaned by the OS; copy the verified artifacts into the actual private deployment record before applying anything. Its keyring is root-only; do not publish it. The workstation holds independent metadata/content review under `/tmp/omarchy-pi-fresh-metadata/` and `/tmp/omarchy-pi-bubblewrap-013/`.

The scratch sync databases match independent downloads from `https://de3.mirror.archlinuxarm.org/aarch64/`:

| Database | SHA-256 |
| --- | --- |
| `core.db` | `8d4be136468cbba36b731e2bf46c7ec065b0854cd1b3ee2c0011cf0a7aee0fef` |
| `extra.db` | `b1f25d3eb896345d7d484a9a20748bb5e1cb396400e14b1378eb5987db85d583` |
| `alarm.db` | `d028400171bd741ec09e77acb178d1f7b92734417a7160722056a0fa1a2924a0` |
| `aur.db` | `bdd038c7c32dc6de8977c65659249f3ca4b498c303ded46750eb6ab6f1dad187` |

A rehearsal also found and corrected a runbook problem: pacman's unprivileged download user could not traverse the proposed mode-0700 backup directory. Resolution/download now uses a separate scratch hierarchy with searchable ancestors and readable metadata/cache directories; recovery backups and the copied keyring remain private. The downloader sandbox and signature policy were not disabled.

## Next bounded action

Complete and verify the recovery backup, check the live prestate still matches this resolution, then use the reviewed 107 local archives for the one transaction with the udev safeguard. The graphical experiment and unattended reboot test remain later checks. Any new package or version drift requires updating the manifest and reviewing that actual difference; it is not a reason to abandon the project.

Sources: [official Bubblewrap package](https://archlinuxarm.org/packages/aarch64/bubblewrap), [ALARM signing policy and fingerprint](https://archlinuxarm.org/about/package-signing), [verified named-mirror archive](https://fl.us.mirror.archlinuxarm.org/aarch64/extra/bubblewrap-0.13.0-1-aarch64.pkg.tar.xz), and the native Pi checks recorded above.
