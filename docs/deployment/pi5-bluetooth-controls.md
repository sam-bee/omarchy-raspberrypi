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

## Acceptance checks

For a target installation, record the selected source revision and the complete signed package transaction in private deployment evidence. Confirm that the pairing agent is enabled and active, that existing bonds are preserved, and that the panel starts discovery when opened and stops it when closed. Use a test Bluetooth sink selected by the operator; do not publish its model, address or pairing details.

Check the panel visually, restore workspace and focus, and verify the normal Bluetooth and audio controls. Test new-device pairing, forgetting and re-pairing, radio power cycling, reboot persistence and physical keyboard delivery separately. Synthetic input may verify registration and IPC behavior, but it does not establish physical keyboard acceptance.
