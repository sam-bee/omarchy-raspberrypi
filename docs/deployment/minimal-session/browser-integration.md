# Minimal-session browser integration — 27 September 2026

The versioned Pi session profile now reserves `Super+B` for the normal browser and `Super+Shift+B` for a private window. Both bindings call Omarchy's `omarchy-launch-browser`, so links opened by Omarchy can follow the XDG default-browser choice. The minimal profile still disables the general application bindings, which include shortcuts for software not installed on this Pi.

The source bindings are in [`install/arm64/session/hyprland.lua`](../../../install/arm64/session/hyprland.lua). The launcher reads `xdg-settings get default-web-browser`, falls back to the HTTPS MIME handler when necessary, and starts the selected browser through a transient user service and `uwsm-app`. Its `--private` option maps to Chromium's `--incognito` flag. The profile also stages [`chromium-flags.conf`](../../../install/arm64/session/chromium-flags.conf) with only `--ozone-platform=wayland` and [`portals.conf`](../../../install/arm64/session/portals.conf) with GTK as the preferred portal backend. The ARM package policy records `chromium`, `xdg-desktop-portal-gtk`, and `ttf-liberation` as candidates.

The normal profile upgrade path is the committed-release workflow in [`pi5-minimal-install.md`](../pi5-minimal-install.md): stop the graphical session, stage the newer clean checkout, inspect the new current/previous release pointers and managed configs, then start the session. The stager accepts existing four-file releases and adds the two browser settings on upgrade. Rolling back to a release that predates them preserves those user settings, while two old releases can still be swapped without creating absent browser files.

After installing Chromium, set the user's default browser and web handlers explicitly:

```bash
env -u BROWSER xdg-settings set default-web-browser chromium.desktop
xdg-mime default chromium.desktop x-scheme-handler/http
xdg-mime default chromium.desktop x-scheme-handler/https
```

Then verify `env -u BROWSER xdg-settings get default-web-browser` and both `xdg-mime query default` results. In the running session, confirm the keybinding list exposes both shortcuts, open an ordinary site with `Super+B`, and confirm `Super+Shift+B` opens a private Chromium window. Test file selection and browser playback separately; package installation alone does not establish portal or audio behavior.

## Pi verification on 27 September

A freshly resolved, signed transaction installed Chromium 153.0.8010.36-1 and its desktop support as 42 additions with no upgrades or removals. The live user configuration has the two bindings, Wayland flag, GTK portal preference and Chromium HTML/HTTP/HTTPS defaults. The user-owned Hyprland config was updated in place and reloaded without changing the staged source pointer or restarting the session. `hyprctl configerrors` was empty, both portal services were active, and the normal Omarchy launcher rendered the Raspberry Pi website in a native Wayland window. The same launcher’s `--private` path rendered an Incognito window. Both were visually inspected; physical key presses and reboot persistence are not claimed by these checks.

Focused source checks passed: `arm64-minimal-session-test.sh`, `arm64-session-upgrade-test.sh`, and `arm64-plan-test.sh`. These cover staging the six files, upgrading and rolling back original four-file releases, preserving user settings, and the selected package policy. Browser media and interactive desktop acceptance are tracked separately from these configuration tests.
