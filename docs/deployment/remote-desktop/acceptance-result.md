# Bounded real-client RDP acceptance — 23 September 2026

The bounded manual RDP test passed on the Raspberry Pi 5 using the pinned `hypr-rdp` v0.1.6 build and staged FreeRDP 3.31. Pi-side evidence is under `/home/sierra/.local/state/omarchy-pi-rdp/20260923T111237Z`. The live session ran on tty8 (session 170): wrapper PID 21385, Hyprland PID 21404, and `hypr-rdp` PID 21952.

The server listened only on `127.0.0.1:3389`; the workstation reached it through an SSH local forward at `127.0.0.1:13389`. The actual FreeRDP X11 window, titled `Omarchy Pi RDP Acceptance`, ran in a private Xvfb display. The captured client-window images are stored outside the repository under `/home/me/.local/state/omarchy-pi-rdp-harness-20260923/captures/`; none are checked in.

The first client showed the live session, displayed the typed marker `RDP_FIRST2_1113`, and produced an observable pointer click on workspace 3. After closing that client while leaving the server and tunnel running, a second client reconnected to the same server and compositor session, displayed `RDP_RECONNECT_1115`, and clicked workspace 4. The monitor list showed the existing 1280x720 `omarchy-pi` output alongside a separate 1280x720 output named exactly `hypr-rdp`. The server log recorded the EGFX ClearCodec fallback, which the FreeRDP client rendered successfully.

The bounded operator completed with exit status 0. Cleanup removed the temporary TLS certificate, private key, and `.tls.lock`; it stopped the test server and session, and verified removal of the RDP output and listener. Earlier attempts stopped on preflight, launch timing, or a monitor-name assertion; the first real-client attempt demonstrated view and input but did not complete the reconnect gate. The successful full run is `20260923T111237Z`.

**Evidence boundary:** this proves a manually started, bounded RDP session with live view, keyboard input, pointer input, reconnect, loopback-only service, and cleanup. It does not prove persistent startup, RDP after reboot, or a completed maintenance update. The final independent protected-state audit is still pending.
