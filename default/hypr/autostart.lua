hl.on("hyprland.start", function()
  -- Slow app launch fix -- set systemd vars before starting session services.
  hl.exec_cmd("systemctl --user import-environment $(env | cut -d'=' -f 1)")
  hl.exec_cmd("dbus-update-activation-environment --systemd --all")

  -- A staged session can run the shell after output creation without invoking
  -- first-run helpers or system integration. Normal Omarchy is unchanged.
  if _G.omarchy_autostart_minimal == true then
    local path = (os.getenv("OMARCHY_PATH") or "/usr/share/omarchy") .. "/install/arm64/session/start-shell.sh"
    hl.exec_cmd("bash " .. o.shell_quote(path))
    return
  end

  hl.exec_cmd("omarchy-launch-shell")
  hl.exec_cmd("omarchy-provision-first-run")
  hl.exec_cmd("omarchy-powerprofiles-init")
  hl.exec_cmd(o.launch("omarchy-hyprland-monitor-watch"))
  hl.exec_cmd(o.launch("udiskie --automount --no-notify --no-tray"))

  -- Run post-boot hooks after startup config has loaded.
  hl.exec_cmd("sleep 2 && omarchy-hook post-boot")
end)
