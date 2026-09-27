-- User-owned, bounded Pi session. Do not run Omarchy's first-run/system setup.
_G.omarchy_autostart_minimal = true
_G.omarchy_default_bindings = false

dofile((os.getenv("OMARCHY_PATH") or "/usr/share/omarchy") .. "/default/hypr/bootstrap.lua")
require("default.hypr.omarchy")
require("default.hypr.bindings.tiling")

hl.monitor({ output = "", mode = "1280x720@60", position = "0x0", scale = 1 })
hl.config({ animations = { enabled = false } })
hl.bind("SUPER + RETURN", hl.dsp.exec_cmd("uwsm-app -- xdg-terminal-exec"), { description = "Open Foot" })
hl.bind("SUPER + B", hl.dsp.exec_cmd("omarchy-launch-browser"), { description = "Browser" })
hl.bind("SUPER + SHIFT + B", hl.dsp.exec_cmd("omarchy-launch-browser --private"), { description = "Browser (private)" })

-- Keep the discoverable Omarchy shortcuts for the two image workflows that
-- are available in the bounded profile: choose a still background, or browse
-- files and open images through the installed MIME handler. The background
-- route deliberately enters the existing selector instead of the full theme
-- switcher; the latter has post-hooks outside this Pi profile.
o.bind("SUPER + CTRL + SPACE", "Background switcher", "omarchy-menu toggle background")
o.bind("SUPER + SHIFT + F", "File manager", { omarchy = "nautilus" })

-- Keep the Pi profile bounded while retaining Omarchy's discoverable media
-- controls. These are the same XF86 bindings used by the full profile, with
-- the unrelated laptop brightness, touchpad, and playback bindings omitted.
o.bind("XF86AudioRaiseVolume", "Volume up", "omarchy-audio-output-volume raise", { locked = true, repeating = true })
o.bind("XF86AudioLowerVolume", "Volume down", "omarchy-audio-output-volume lower", { locked = true, repeating = true })
o.bind("XF86AudioMute", "Mute", "omarchy-audio-output-volume mute-toggle", { locked = true })
o.bind("ALT + XF86AudioRaiseVolume", "Volume up precise", "omarchy-audio-output-volume +1", { locked = true, repeating = true })
o.bind("ALT + XF86AudioLowerVolume", "Volume down precise", "omarchy-audio-output-volume -1", { locked = true, repeating = true })
o.bind("SUPER + CTRL + A", "Audio", "omarchy-shell shell toggle omarchy.audio")
o.bind("SUPER + CTRL + B", "Bluetooth", "omarchy-shell shell toggle omarchy.bluetooth")

-- Everyday actions use the existing Omarchy overlays. Keep the launcher on
-- installed applications while the broader system/setup menus are deferred.
o.bind("SUPER + SPACE", "Apps menu", "omarchy-menu toggle apps")
o.bind("SUPER + ALT + SPACE", "Apps menu", "omarchy-menu toggle apps")
require("default.hypr.bindings.clipboard")
o.bind("PRINT", "Screenshot", "omarchy-capture-screenshot")
o.bind("SHIFT + PRINT", "Full-screen screenshot", "omarchy-capture-screenshot fullscreen")

o.bind("SUPER + comma", "Dismiss last notification", "omarchy-shell notifications dismissOne")
o.bind("SUPER + SHIFT + comma", "Dismiss all notifications", "omarchy-shell notifications dismissAll")
o.bind("SUPER + CTRL + comma", "Toggle silencing notifications", "omarchy-shell notifications toggleDnd")
o.bind("SUPER + ALT + comma", "Invoke last notification", "omarchy-shell notifications invokeLast")
o.bind("SUPER + SHIFT + ALT + comma", "Open notification history", "omarchy-shell notifications showHistory")

-- Upstream desktop controls supported by the Pi's Wayland session.
o.bind("SUPER + SHIFT + CTRL + SPACE", "Theme menu", "omarchy-menu toggle theme")
o.bind_toggle("SUPER + CTRL + N", "Toggle nightlight", "nightlight")
o.bind_toggle("SUPER + CTRL + I", "Toggle locking on idle", "idle")
o.bind("SUPER + CTRL + L", "Lock screen", "omarchy-system-lock")
o.bind("SUPER + CTRL + W", "Network", "omarchy-shell shell toggle omarchy.network")
o.bind_toggle("SUPER + SHIFT + SPACE", "Toggle top bar", "bar")
o.bind("XF86AudioMicMute", "Mute microphone", "omarchy-audio-input-mute", { locked = true })
o.bind("XF86AudioNext", "Next track", "omarchy-shell media next", { locked = true })
o.bind("ALT + XF86AudioPlay", "Next track", "omarchy-shell media next", { locked = true })
o.bind("XF86AudioPause", "Pause", "omarchy-shell media playPause", { locked = true })
o.bind("XF86AudioPlay", "Play", "omarchy-shell media playPause", { locked = true })
o.bind("XF86AudioPrev", "Previous track", "omarchy-shell media previous", { locked = true })
o.bind("ALT + SHIFT + XF86AudioPlay", "Previous track", "omarchy-shell media previous", { locked = true })
o.bind("SHIFT + XF86AudioMute", "Switch audio output", "omarchy-audio-output-switch", { locked = true })
o.bind("SHIFT + XF86AudioPause", "Switch media source", "omarchy-audio-source-switch", { locked = true })
o.bind("SHIFT + XF86AudioPlay", "Switch media source", "omarchy-audio-source-switch", { locked = true })
