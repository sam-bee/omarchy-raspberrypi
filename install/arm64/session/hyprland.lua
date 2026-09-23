-- User-owned, bounded Pi session. Do not run Omarchy's first-run/system setup.
_G.omarchy_autostart_minimal = true
_G.omarchy_default_bindings = false

dofile((os.getenv("OMARCHY_PATH") or "/usr/share/omarchy") .. "/default/hypr/bootstrap.lua")
require("default.hypr.omarchy")

hl.monitor({ output = "", mode = "1280x720@60", position = "0x0", scale = 1 })
hl.config({ animations = { enabled = false } })
hl.bind("SUPER + RETURN", hl.dsp.exec_cmd("uwsm-app -- xdg-terminal-exec"), { description = "Open Foot" })
