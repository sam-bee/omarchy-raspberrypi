# USB package-maintenance rehearsal — 26 September 2026

A complete signed package upgrade succeeded on the disposable Arch Linux ARM USB installation on the same Raspberry Pi 5. The system subsequently booted `linux-rpi 6.18.53-1-rpi`, restored SSH/networking and its minimal UWSM/Hyprland/Quickshell desktop, and retained the selected PCIe Gen1 link and USB-first EEPROM order. The encrypted NVMe installation was not upgraded.

This is one same-board USB upgrade/reboot result. It does not establish NVMe encrypted-root update acceptance, a complete Omarchy updater, independent reproduction or long-term reliability. Restoring the recovery backup and booting the restored system remain untested. RDP was not repeated in this run; the USB installation's earlier test profile had been removed after its original acceptance.

## Transaction and recovery preparation

The USB installation started with 345 packages and a clean pacman database. A scratch copy of its own local database and trusted keyring resolved the full upgrade against refreshed repositories. All 11 candidate archives and their exact previous versions passed signature verification with that keyring; the local-archive preview matched the complete resolver result. There were no package additions or removals. The EEPROM package was not installed on this USB system and was not part of its transaction.

| Package | Previous | Installed |
| --- | --- | --- |
| hyprland-guiutils | 0.2.2-3 | 0.2.2-4 |
| hyprtoolkit | 0.5.4-6 | 0.6.0-1 |
| libdwarf | 1:2.3.2-1 | 1:2.3.3-1 |
| libpipewire | 1:1.6.8-1 | 1:1.6.9-1 |
| linux-rpi | 6.18.52-1 | 6.18.53-1 |
| systemd | 261.3-1 | 262-1 |
| systemd-libs | 261.3-1 | 262-1 |
| systemd-resolvconf | 261.3-1 | 262-1 |
| systemd-sysvcompat | 261.3-1 | 262-1 |
| util-linux | 2.42.3-1 | 2.42.4-1 |
| util-linux-libs | 2.42.3-1 | 2.42.4-1 |

Before the transaction, the clean offline USB root was archived with numeric ownership, hard links, sparse-file handling, ACLs and xattrs, then compared against its read-only source. The boot partition was imaged and independently compared by hash; partition metadata and the MBR/gap were also retained. These backups were encrypted off-machine and independently decrypted, decompressed and hash-verified before proceeding. This is a complete logical root-file backup plus a raw boot image, not a forensic copy of unused ext4 blocks. It is verified backup material, not a completed restore test.

The USB boot configuration was first brought into line with the accepted Gen1 policy. Its original config was preserved; only an explanatory comment and `dtparam=pciex1_gen=1` were added under the existing final `[all]` section. A clean read-only remount verified that edit. The running NVMe configuration and EEPROM order were not changed.

## Execution and boot checks

The exact reviewed archives were installed in one ordinary pacman transaction, with normal dependency/signature checks and a second SSH session. All 11 versions matched the expected result; `pacman -Dk` passed. Checked account files, SSH host keys/configuration, PAM files, network/Wi-Fi configuration, fstab, mkinitcpio configuration and boot config/cmdline retained their pre-transaction hashes. No boot `.pacnew` appeared.

The systemd transaction ran its normal sysusers, tmpfiles, sysctl, manager reload/reexec and udev hooks. The kernel hook generated the new initramfs. It warned about missing firmware for the included `xhci_pci_renesas` module; the subsequent Pi USB-root reboot passed. This warning is not evidence that unrelated Renesas controllers are supported.

Before reboot, the installed kernel image matched the signed package byte-for-byte. The generated initramfs identified kernel `6.18.53-1-rpi`. Required USB/storage/filesystem drivers were verified either in the initramfs or in the package's matching `modules.builtin`: USB storage, ext4 and SCSI disk support are built into this kernel. An initial check that required separate initramfs module files was therefore too strict; verifying both locations resolved it without rebuilding or modifying the image.

The post-upgrade boot had a new boot ID, the expected USB root and boot partition, kernel `6.18.53-1-rpi`, actual NVMe PCIe link at 2.5 GT/s ×1 and unchanged `BOOT_ORDER=0xf164`. Fresh key-authenticated SSH and sudo worked. The UWSM session, Hyprland and Quickshell were active, with no failed system units, current-boot core events or driver-reported NVMe/PCIe/I/O alerts in the checks.

A Foot window mapped on the headless 1280×720 output, displayed the new kernel version and ran for 60 seconds. Its screenshot was visually checked, then the window exited normally. Hyprland reported no configuration errors before or after, and there were no current-boot core events afterward. The existing centered Hyprland logo remains visible; this run does not claim to resolve that previously recorded visual limitation.

## Remaining gates

- Restore the captured USB root and boot state, then prove that the restored system boots and its package/configuration state matches the baseline.
- Repeat actual-client RDP interaction and reconnect as part of broader maintenance acceptance.
- Review a separate complete NVMe transaction and repeat encrypted-root boot/access acceptance there; do not infer it from this unencrypted USB root.

Preserve the selected PCIe generation, existing authentication and recovery media throughout these steps. This rehearsal used the [supervised maintenance gates](README.md); it does not make the general Omarchy updater ARM-safe.
