# Raspberry Pi network adapter policy

For a Pi with a damaged onboard antenna, NetworkManager can exclude that radio and select the USB Wi-Fi adapter by its permanent hardware MAC. The existing Wi-Fi connection profile remains the source of the SSID and authentication material. This is a machine-specific hardware policy, not a default exclusion of onboard Wi-Fi on every Pi.

Resolve the permanent MACs from the running machine before applying this policy. Do not infer them from a current interface name or a randomized current MAC. Keep the values machine-local; the placeholders below are intentionally not real profile UUIDs or MAC addresses.

## Persistent policy

Modify the existing Wi-Fi profile by its exact UUID and change only these NetworkManager properties:

```sh
sudo nmcli connection modify uuid "$PI_WIFI_PROFILE_UUID" \
  802-11-wireless.mac-address "$PI_USB_WIFI_MAC" \
  802-11-wireless.powersave 2 \
  connection.autoconnect yes \
  connection.autoconnect-retries 0
```

Set `PI_WIFI_PROFILE_UUID` and `PI_USB_WIFI_MAC` to the verified existing profile UUID and permanent USB MAC first. This preserves the profile UUID, its SSID, and its `[wifi-security]` authentication material. Do not recreate, delete, or replace the profile. The profile keyfile under `/etc/NetworkManager/system-connections/` must remain root-owned and mode `0600`; make a root-only local backup before changing it. Power-save value `2` disables Wi-Fi power saving; autoconnect retry value `0` means retry forever.

Keep Wi-Fi power saving disabled globally with `/etc/NetworkManager/conf.d/omarchy-wifi-powersave.conf`:

```ini
[connection]
wifi.powersave = 2
```

Exclude the damaged onboard adapter with `/etc/NetworkManager/conf.d/90-omarchy-pi-wifi-adapter.conf`:

```ini
[device-omarchy-pi-wifi-adapter]
match-device=mac:<DAMAGED_ONBOARD_PERMANENT_MAC>
managed=0
```

The device match uses the permanent MAC and does not depend on whether the kernel currently calls the interfaces `wld0`, `wlu2`, or another name. The exclusion drop-in is staged separately from activation so an existing SSH session can continue over the current radio.

## Apply and verify

Use `nmcli` for the profile edit and atomic writes for the drop-ins, then inspect the resulting files and `nmcli connection show uuid "$PI_WIFI_PROFILE_UUID"`. On the tested Pi, `nmcli general reload` read the configuration but the onboard device remained managed until NetworkManager restarted. Schedule any reconnect or service restart inside a guarded rollback procedure, after preparing access through the USB adapter. Writing the policy and activating it are separate steps because activation can interrupt SSH.

After the guarded restart/handover, verify that the selected profile reports the USB permanent MAC, powersave is `2` (NetworkManager may display `2 (disable)`), autoconnect retries is `0`, the USB device is connected, and the excluded device reports `managed=no`. Confirm that the profile's UUID and authentication material are unchanged.

Keep Wi-Fi secrets and authentication keyfiles out of Git. The non-secret drop-ins and deployment procedure can be versioned; record machine-specific MACs and profile UUIDs in the machine's inventory. The live settings belong under `/etc/NetworkManager/`, rather than a desktop user's `~/.config/`.
