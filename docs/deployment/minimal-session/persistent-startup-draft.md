# Persistent session startup runbook

This runbook describes the checks required before enabling a Pi graphical session at boot. It is intentionally account and machine independent. Supply the target user, home, UID, VT, source revision, package versions, RDP profile and private recovery paths from the operator's private run record.

First pass the package and bounded-session gates in [package-stage.md](package-stage.md) and [session-stage.md](session-stage.md). Keep an independent recovery connection, a known-good host key, a verified encrypted recovery baseline and physical or rescue access available.

Install the reviewed system and user units with the correct ownership and modes. Keep the system tty service disabled until the manual start/stop trial passes. Keep the user RDP service disabled or inactive until its credential file and loopback-only listener have been checked. Do not enable lingering, automatic login, a display manager or a broad network listener as part of this procedure.

The manual trial must prove that the unit opens one local PAM-backed session on the chosen VT, starts the reviewed UWSM/Hyprland wrapper, starts the intended shell and RDP service, and stops cleanly through the same tty hangup path. Verify no unexpected session, graphical unit, Wayland socket, listener or process remains afterward.

Only after a fresh encrypted baseline passes those checks may the system unit be enabled and a controlled reboot performed. After reboot, verify a changed boot ID, encrypted root and boot mount where configured, network and fresh SSH, the single local graphical session, the compositor and shell, the loopback-only RDP listener and stable TLS identity. Record all results privately.

Never put the RDP password, TLS key, recovery key, Wi-Fi configuration or exact host state in this repository. Do not make boot-order changes, format storage, rebuild the initramfs or restore a whole recovery archive as part of this gate.
