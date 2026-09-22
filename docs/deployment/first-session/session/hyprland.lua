-- Temporary compositor smoke test; no Omarchy startup or provisioning.
local terminal = "foot --config=/home/sierra/.config/omarchy-pi-smoke/foot.ini"

hl.monitor({ output = "", mode = "1280x720@60", position = "0x0", scale = 1 })
hl.config({ animations = { enabled = false } })
hl.bind("SUPER + RETURN", hl.dsp.exec_cmd(terminal), { description = "Open Foot" })
hl.on("hyprland.start", function()
  hl.exec_cmd(terminal)
end)
