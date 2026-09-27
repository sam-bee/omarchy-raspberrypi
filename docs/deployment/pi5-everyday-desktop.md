# Pi everyday desktop

The bounded Pi session exposes the installed application launcher, universal clipboard shortcuts and history, screenshots, notifications and the upstream Bluetooth panel. It retains the accepted theme, browser, Files and audio controls. This profile does not run the full upstream installer or enable unrelated system setup, authentication, lock or network services.

The goal is the upstream Omarchy user experience on supported Pi hardware. This reduced profile is an incremental porting mechanism; exclusions are compatibility or validation gaps to resolve, rather than a separate desktop design.

## Shortcuts

| Shortcut | Action |
| --- | --- |
| Super+Space or Super+Alt+Space | Search installed applications |
| Super+C / Super+V | Copy / paste; terminal windows receive Ctrl+Shift+C / Ctrl+Shift+V |
| Super+Ctrl+V | Clipboard history |
| Super+Ctrl+B | Bluetooth panel |
| Print | Select a screenshot region; Escape cancels |
| Shift+Print | Full-screen screenshot |
| Super+comma | Dismiss last notification |
| Super+Shift+comma | Dismiss all notifications |
| Super+Ctrl+comma | Toggle notification silencing |
| Super+Alt+comma | Invoke the last notification action |
| Super+Shift+Alt+comma | Notification history |

The Apps menu deliberately opens the existing installed-applications view. The broader setup and system menu is outside this profile. Clipboard history uses Omarchy's Quickshell overlay and its text/PNG watchers; it does not require a separate history daemon. Notifications use the existing Quickshell notification server and history panel.

The `everyday-desktop` package profile in `install/arm64/plan.py` records the runtime roots: `gtk3`, `wl-clipboard`, `wtype`, `grim`, `slurp`, `libnotify`, `jq`, and `python`. Where Omasnap is unavailable, the screenshot command uses grim and slurp to save a PNG and copy it to the clipboard; annotation and scrolling capture require Omasnap.

## Deployment and acceptance

Stage a clean committed release through `install/arm64/stage-user-session.sh`, retaining the previous release for rollback. Reload Hyprland and restart Quickshell in the existing Wayland session after the separately reviewed package transaction. Verify live launcher selection, text transfer between applications, history selection, PNG clipboard content, screenshot output and cancellation, and notification display, history and dismissal. A compositor reload and shell restart do not establish reboot persistence or physical keyboard acceptance.
