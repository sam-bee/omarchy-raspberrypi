# Shell configuration startup deployment — 25 September 2026

The startup-ordering fix is deployed to the existing minimal Pi desktop. Quickshell was restarted successfully; Hyprland and the RDP server stayed running. The workspace/clock bar rendered before and after the switch, shell IPC responded, and a follow-up query found no new coredumps. The pre-existing missing host-portal warning remains.

## Exact scope

The deployed source is `d30f7aa94f2e8c516635c88b490a0420e02150da` on `deploy/shell-config-startup`. Its parent is the previously deployed `2b5fe5bef3b2971ef970099a6e5794519e191036`. This adds only the four-line shell fix and its regression fixtures; it does not deploy intervening main-branch changes. The same fix is on `quattro-rpi5` as `90c97494567a7c5527a496d3228acc841245e9d3`. Both branches are pushed to the project fork.

Before staging, all 1,887 files in the active release matched the parent Git tree's blob hashes and executable modes. A separate release copy received the six changed/added files and an updated source-commit marker. All 1,892 files then matched the deployment commit. The original release was retained intact.

The normal session staging procedure stops the whole graphical session because it can change compositor and user configuration. This deployment used a narrower, shell-only switch: all other runtime files and user configuration payloads were identical. Under the staging lock, the old shell exited through its IPC interface, its launcher exited, the release pointers were replaced atomically, and Hyprland launched the replacement through `omarchy-launch-shell`. The deployment harness included rollback on failed startup; that path was not exercised because startup passed.

## Acceptance and limits

- Initial loading resolves configuration before plugin discovery; the Pi's existing minimal-session guards remain intact.
- A single replacement shell process stayed alive and answered `ping` and `debugBarGeometry`; clock and workspace widgets had positive dimensions and were visible in the captured frame.
- The user's disabled plugin entries remained disabled according to IPC. The minimal-session service bypass also remains active, so this live smoke is not independent proof of disabled-service startup prevention; that behavior was demonstrated by the earlier isolated regression checks.
- The boot ID, Hyprland process and RDP process were unchanged; the RDP user service stayed active. This run did not repeat RDP client interaction.
- Watched shell, Hyprland and UWSM user configuration hashes were unchanged. Hyprland reported no configuration errors.
- No package, credential, authentication-policy, boot-order or system-unit changes were made. No reboot was performed, so this does not add reboot acceptance or resolve the separate read-path stability investigation.

## Rollback

The release root is `$HOME/.local/share/omarchy-pi`:

- `current` points to `releases/d30f7aa94f2e8c516635c88b490a0420e02150da`.
- `previous` points to `releases/2b5fe5bef3b2971ef970099a6e5794519e191036`.

For a targeted rollback, first confirm those exact pointers and an unlocked desktop. Stop only the current Quickshell through IPC before changing `current` (configuration path resolution can otherwise select the wrong instance). Atomically restore `current` to the retained old release, then launch through Hyprland using the current session environment. Verify IPC, bar geometry and a captured frame again. The original `previous` pointer was absent; restore that state after a successful rollback. Do not remove either release as part of recovery.

Private deployment evidence includes before/after frames, full release manifests, source hashes, configuration hashes, process/boot state and the startup journal. No upstream PR was opened.
