# Persistent session acceptance record

The machine-specific startup and reboot result is retained in the private deployment evidence for the run. A public result must contain only the checks and limits needed to reproduce the acceptance.

Require a fresh encrypted recovery baseline, a manual start/stop trial and a controlled reboot. Record privately whether the boot ID changed, encrypted root and boot mounts returned, SSH and network returned, the chosen local session and compositor started once, the shell rendered, the RDP listener remained loopback-only, clients could reconnect and the TLS identity stayed stable.

This result does not authorize package upgrades, a moving source branch, a public RDP listener or boot-order changes. Repeat the full gate after any package, unit, source or credential change.
