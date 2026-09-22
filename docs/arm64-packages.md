# ARM64 package policy

This policy is the first bounded package plan for the official Omarchy source at upstream HEAD `947e2fc0`. The authoritative input is [`install/omarchy-base.packages`](../install/omarchy-base.packages); [`install/arm64/packages.tsv`](../install/arm64/packages.tsv) contains one row for every non-comment entry in that file. Additional runtime candidates, including dependencies normally supplied by the ISO, are recorded separately in [`install/arm64/packages-extra.tsv`](../install/arm64/packages-extra.tsv).

The TSV files are planning data, not an installer. A [read-only Pi package audit on 22 September 2026](arm64-package-audit.md) resolved the current candidate and replacement names against the Pi's existing sync databases and corrected four policy rows. Those databases can change; every candidate and replacement needs fresh target-local resolution and hook review before a transaction is prepared. The replacement rows express package-name decisions for the ARM plan; they do not establish that the replacement has been runtime-tested on the Pi.

The initial candidate set covers compositor/session and shell runtime, terminal, portals, fonts, clipboard, basic audio, package tooling, and SSH-friendly administration tools. Nautilus is deferred because its current dependency closure installs `mdadm` and triggers the Pi's mkinitcpio hook; the graphical file-manager action remains a known gap. Applications, developer toolchains, media production, indexing, printing, Docker, Bluetooth, optional services, and AUR/build workflows are deferred until the desktop is working and their ARM source and runtime behavior have been checked.

Several upstream features have an explicit gap in this first policy. `ttfx` is excluded because the shipped screensaver invokes it and no ARM-compatible replacement or tested fallback is part of this milestone. The screen-share picker is deferred until the portal's own fallback is validated. `omasnap` is an optional screenshot annotation tool, not a filesystem snapshot facility. OWE and its lock-feed module are deferred together; video backgrounds and the lock-feed shader/session path are not claimed until their ARM runtime is tested.

`networkmanager`, `wireless-regdb`, `ufw`, `sddm`, `plymouth`, `kernel-modules-hook`, and other boot/network or service ownership changes are excluded from the first transaction. The Pi already has a working `systemd-networkd` plus `wpa_supplicant@wld0` path, SSH access, encrypted NVMe root, USB-key unlock, Raspberry Pi firmware, `linux-rpi`, and a deliberately boot-tested initramfs. This policy does not replace or reconfigure any of those components. In particular, it does not add a generic kernel, Limine, Snapper, firewall, NetworkManager, display manager, or initramfs hook. `avahi` is an explicit candidate only because the current `pipewire-pulse` package requires it; enabling or changing Avahi services is outside this layer.

`mesa` and `vulkan-broadcom` are separate ARM/Pi extras because they are absent from the official base list. The same extras file records audio/Wayland and runtime candidates, including packages that the ISO list carries separately, without making them part of the base-list coverage claim. Their candidate labels identify what the first desktop may need; they still require package-manager and runtime verification.

The third-party ARM branch was consulted only as historical evidence for package-name substitutions and omissions. Its installer and manifests are not the source of this policy. The initial policy was offline; the later audit used read-only Pi access. Neither stage installed packages.

## Action labels

- `candidate`: consider for the bounded first desktop transaction after target-local resolution.
- `replace`: use the `replacement` name in the ARM plan, then resolve it at deployment.
- `defer`: keep out of first bring-up and revisit after the core session works.
- `exclude`: do not include in the first transaction because it is protected infrastructure, an out-of-scope platform feature, a separate bootstrap decision, or has no established ARM source for this plan.
