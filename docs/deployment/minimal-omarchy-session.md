# Draft Pi UWSM and Quickshell session

This is a source-only draft for the step after the bounded Hyprland/Foot smoke. It has not been installed or launched on the Pi. Fresh package closure, archive/hook review, and the smoke result must precede deployment. It does not install packages, enable services, change boot/network configuration, or arrange automatic login.

## Scope and source layout

`install/arm64/stage-user-session.sh` runs as `sierra`, from a clean committed checkout on the target. It archives that exact commit to `~/.local/share/omarchy-pi/releases/<commit>/`, creates `~/.local/share/omarchy-pi/current` as a symlink to the release, and installs four new user-owned files. It refuses an existing release, current link, or destination file, including dangling symlinks. The script has no `sudo`, `pacman`, or `systemctl` action.

| New user file | Purpose |
| --- | --- |
| `~/.config/uwsm/env.d/90-omarchy-pi` | Set `OMARCHY_PATH`, prepend its `bin`, select `xdg-terminal-exec` |
| `~/.config/hypr/hyprland.lua` | Load Quattro's Lua defaults with bounded startup and a Foot terminal binding |
| `~/.config/omarchy/shell.json` | Clock/workspace bar; disable `omarchy.idle` while its screensaver is unavailable |
| `~/.config/xdg-terminals.list` | Select `foot.desktop` |

[UWSM sources user `env.d` files after lower-priority data directories](https://github.com/Vladimir-csp/uwsm/blob/master/README.md), so the user-owned release path overrides a packaged default if present. The session does not rely on `/usr/share/omarchy`, `/etc/omarchy.conf`, or a display manager. The source archive contains the committed Omarchy tree, including commands, shell plugins, Lua defaults, themes, and config assets; it excludes Git internals and unrelated files outside this repository.

The normal Omarchy autostart remains unchanged by default. The Pi overlay sets `omarchy_autostart_minimal` before loading `default.hypr.omarchy`. In that mode startup imports the graphical environment into the user manager and D-Bus and launches the single Omarchy Quickshell process. It skips first-run provisioning, power-profile initialization, monitor watcher, `udiskie`, and post-boot hooks. The overlay disables bundled keybindings and supplies only `Super+Return` through `uwsm-app -- xdg-terminal-exec`, avoiding actions for applications absent from this package stage.

`omarchy.idle` is disabled through the shell's supported `disabledPlugins` setting. This prevents the excluded `ttfx` screensaver and the unvalidated lock path from starting. A persistent interactive deployment needs a tested idle/lock replacement before this setting is removed or the Pi is used unattended. The initial bar excludes the NetworkManager-backed network widget, audio until PipeWire is validated, and menu actions that still expose x86/network setup paths.

## Local validation and later Pi acceptance

Run `bash test/shell.d/arm64-minimal-session-test.sh` from the draft branch. The test stages into a temporary home, verifies exact files and link, checks the shell config, and verifies refusal to overwrite a second time. It does not start a compositor. The Pi package stage must include `uwsm`, `quickshell`, `xdg-terminal-exec`, `inotify-tools`, and Qt Wayland support, with exact dependency closure and hooks reviewed there. The previous smoke stage already supplies Hyprland, Foot, and the terminal font if it passes.

Before a bounded tty8 UWSM launch, validate `Hyprland --verify-config --config ~/.config/hypr/hyprland.lua`, `xdg-terminal-exec --print-id`, the UWSM environment, and preservation of SSH/networkd. During the launch check `hyprctl configerrors`, the compositor's monitor/client state, `omarchy-shell shell ping`, bar/notifications logs, and a fresh independent SSH connection. Stop the named experiment, restore its VT, and repeat substrate checks. No graphics or Quickshell runtime result is claimed by this draft.

## Removal

After stopping only the test session, inspect the four user files and the `current` symlink target. If they still match this staged release, remove those five paths and `releases/<commit>` only. Leave any user-edited file for review instead of deleting it. The stage command creates no package or system service state to undo. Removing the overlay does not roll back the separately reviewed package transaction.
