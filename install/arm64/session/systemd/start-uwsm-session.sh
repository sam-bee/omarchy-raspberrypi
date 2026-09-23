#!/bin/bash

set -euo pipefail

fail() {
  printf 'omarchy-pi session: %s\n' "$1" >&2
  exit 1
}

if (( $# != 1 )); then
  fail 'expected the selected login name from the systemd template instance'
fi

selected_user=$1
if [[ ! $selected_user =~ ^[a-z_][a-z0-9_-]*$ ]]; then
  fail 'template instance is not a supported login name'
fi
if (( EUID == 0 )); then
  fail 'refusing to start a desktop session as root'
fi

if ! account_record=$(/usr/bin/getent passwd "$selected_user"); then
  fail "login $selected_user is absent from the passwd database"
fi
if [[ $account_record == *$'\n'* ]]; then
  fail 'passwd lookup returned more than one account'
fi
IFS=: read -r account_name _ account_uid _ _ account_home _ <<< "$account_record"
if [[ $account_name != "$selected_user" || ! $account_uid =~ ^[0-9]+$ ]]; then
  fail 'passwd lookup returned an invalid account record'
fi
if [[ $account_uid != "$EUID" ]]; then
  fail 'template instance name does not match the service UID'
fi
if [[ $account_home != /* || ! -d $account_home || -L $account_home ]]; then
  fail 'passwd home is not an absolute, real directory'
fi
if [[ ${HOME:-} != "$account_home" ]]; then
  fail 'HOME differs from the selected account home'
fi
if [[ ${LOGNAME:-} != "$selected_user" ]]; then
  fail 'LOGNAME differs from the selected account'
fi
if [[ -n ${USER:-} && $USER != "$selected_user" ]]; then
  fail 'USER differs from the selected account'
fi

runtime_dir="/run/user/$account_uid"
if [[ -n ${XDG_RUNTIME_DIR:-} && $XDG_RUNTIME_DIR != "$runtime_dir" ]]; then
  fail 'XDG_RUNTIME_DIR differs from the selected account runtime directory'
fi
runtime_owner=$(/usr/bin/stat -c '%u' -- "$runtime_dir") || fail 'cannot inspect the account runtime directory'
runtime_mode=$(/usr/bin/stat -c '%a' -- "$runtime_dir") || fail 'cannot inspect the account runtime directory mode'
if [[ ! -d $runtime_dir || -L $runtime_dir || $runtime_owner != "$account_uid" || $runtime_mode != 700 ]]; then
  fail 'account runtime directory is not a private directory owned by the selected user'
fi

bus_address="unix:path=$runtime_dir/bus"
if [[ -n ${DBUS_SESSION_BUS_ADDRESS:-} && $DBUS_SESSION_BUS_ADDRESS != "$bus_address" ]]; then
  fail 'DBUS_SESSION_BUS_ADDRESS differs from the selected account bus'
fi

export HOME="$account_home"
export XDG_RUNTIME_DIR="$runtime_dir"
export DBUS_SESSION_BUS_ADDRESS="$bus_address"
export XDG_SESSION_TYPE=wayland
export XDG_SESSION_DESKTOP=Hyprland
export XDG_CURRENT_DESKTOP=Hyprland
export AQ_NO_KMS_REQUIREMENT=1

exec /usr/bin/uwsm start -g -1 -U run -e -D Hyprland -- /usr/bin/start-hyprland -- --config "$account_home/.config/hypr/hyprland.lua"
