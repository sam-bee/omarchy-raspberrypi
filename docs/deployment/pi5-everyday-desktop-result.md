# Pi everyday desktop deployment — 27 September 2026

The bounded everyday desktop profile is deployed as `82de4d6e04ec8f2127e8ab29f997d080bcde3ae6`, with `292db52f03f72760ccbdb7912841bf7a4b5e066c` retained for session rollback. The implementation commits are `c11e8551` (basic screenshot fallback), `292db52f` (Pi launcher, clipboard and notifications), and `82de4d6e` (freeform selector input handling).

Four signature-verified packages were added: `wl-clipboard 1:2.3.0-1`, `wtype 0.4-2`, `slurp 1.5.0-2`, and `libnotify 0.8.8-1`. The package count increased from 581 to 585 with no upgrades or removals. Existing grim, GTK, jq and Python supplied the other runtime dependencies. The transaction preserved boot files, EEPROM configuration, encryption metadata, authentication and networking; only the expected generated linker cache changed among the checked system configuration files. The source stage changed the two intended user configuration files, reloaded Hyprland and restarted Quickshell without rebooting.

## Validation

The Apps overlay was visually inspected, searched for Foot and activated the existing client. Backspace/Left stayed within the installed-applications view. Notification delivery, dismissal, history replay and restoring the original do-not-disturb state passed functional checks. A synthetic notification was visually inspected in a cropped capture from an empty temporary workspace; text, spacing and placement were correct.

Chromium-to-Foot text transfer passed. The clipboard overlay searched for a seeded history entry and its normal Enter action automatically pasted into a prepared Foot receiver; the resulting text matched exactly. A 1280×720 screenshot produced identical SHA-256 hashes in its saved PNG, Wayland clipboard and clipboard-history image.

A window region was captured by moving the pointer and invoking the existing region-selection helper. The generated clipboard, full-screen and region images were visually inspected. Freeform selection initially waited for EOF on inherited SSH input instead of mapping its layer. The helper now closes that unused input. After deployment, the public screenshot command was deliberately given an open input pipe: its selector appeared, Escape naturally exited with status 0, and clipboard types and content were unchanged.

Final health passed with 585 packages, two clipboard watchers, no new cores, no failed units and no compositor configuration errors. Both staged user configuration hashes match. Subsequent helper-only staging verified the new release marker, the same boot/background, a responding shell and empty configuration errors without restarting the shell. Boot, EEPROM, LUKS, SSH and networking remain intact. Chromium cache, session and metrics files changed during live browser use and were recorded separately from protected system configuration.

Focused package-plan, session staging/upgrade, clipboard, app-search, notification, Pi service-policy and screenshot-fallback tests passed. Native Omasnap argument delegation checks passed; the existing broader Omasnap test then stopped on an unrelated migration-file mode mismatch in the workstation checkout (`664` versus expected `644`).

## Limits

The screenshot fallback supports basic selection, full-screen capture, saving and copying. Omasnap annotation and scrolling capture remain deferred. Commands were exercised in the active Wayland session and compositor binding registrations were checked; physical keyboard input, workstation-to-Pi RDP clipboard transfer and reboot persistence were not accepted by these tests. The running kernel remains `6.18.52-1-rpi`.

See [shortcuts and profile](pi5-everyday-desktop.md) for normal use.
