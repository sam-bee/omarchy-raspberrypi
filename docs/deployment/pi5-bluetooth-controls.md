# Pi Bluetooth controls

The Pi profile exposes Omarchy Quattro's existing Bluetooth bar widget and `Super+Ctrl+B` shortcut. The panel, model and Bluetooth device/power helpers are unchanged from the upstream checkout. This is part of progressing toward the upstream desktop experience on supported Pi hardware.

## Dependencies and user service

The runtime needs BlueZ, `bluez-utils`, the Quickshell Bluetooth module and the upstream pairing agent. `bluez-tools` supplies `/usr/bin/bt-agent`. On an existing Pi, review its signed package transaction independently of session staging; do not run the whole upstream first-run installer to enable one service.

After the package is installed, install the unchanged `default/systemd/user/bt-agent.service` from the selected source release into the user's `~/.config/systemd/user/` directory if no existing unit would be overwritten. Then, as that user:

```bash
systemctl --user daemon-reload
systemctl --user enable --now bt-agent.service
systemctl --user is-enabled bt-agent.service
systemctl --user is-active bt-agent.service
systemctl --user show -p FragmentPath bt-agent.service
```

The Pi session stager does not install packages or manage this service. Source release rollback therefore does not remove the separately installed pairing agent.

## Deployment result — 27 September 2026

Source release `b9382381e552e952aea06b6983880a3bbc919709` is active, with `82de4d6e04ec8f2127e8ab29f997d080bcde3ae6` retained as the previous session release. The signature-verified `bluez-tools 0.2.0-6` ARM package was the only addition (585 to 586 packages), with no upgrades or removals. The unchanged upstream user unit is enabled and active. Its SHA-256 is `0406b577a1225dc2a9f86638d3c346eb3635168576f04050be50ebcc0be6be12`.

The panel displays the connected Anker SoundCore. Live IPC checks verified discovery starts when the panel opens and stops when it closes. The speaker remained paired, bonded, trusted and connected at 48% volume. The panel's visual contents were inspected. Workspace and focus were restored after testing. Focused Bluetooth, Pi audio-controls and minimal-session tests passed. The final live check found healthy Bluetooth, audio, SSH, networking and desktop services, no failed units, no compositor errors and the same boot/kernel; no reboot occurred.

Agent startup was followed by `Pairable` changing from false to true. `Discoverable` remained false, and existing bonds were preserved. Do not infer from the upstream unit comment that this version limits pairability to the panel's open lifetime: that behavior was not observed.

New-device pairing, forgetting/re-pairing, radio power cycling and reboot persistence remain untested. The standard shortcut is registered, but synthetic key injection did not open the panel, so physical keyboard acceptance is also outstanding. These limits do not affect the verified panel IPC and discovery lifecycle.
