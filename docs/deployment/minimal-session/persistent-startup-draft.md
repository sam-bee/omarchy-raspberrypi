# Persistent tty8 and RDP startup proposal

**Status: manual lifecycle passed; boot startup remains disabled.** The real FreeRDP acceptance run `20260923T111237Z` proved live view, keyboard and pointer input, reconnect, loopback-only listening, and bounded cleanup. Pi-native unit verification and the amended unit's manual start/stop trial passed. The RDP server listened only on loopback, TLS certificate and key hashes matched the earlier accepted values, and the stop returned `Result=success`; the tty8 PAM session, graphical units, Wayland socket, and listener were gone, and tty1 was restored. The first stop attempt at `11:54:15 UTC` sent SIGHUP to UWSM before the then-configured `ExecStop`; UWSM cleaned up its graphical services, but the concurrent stop helper failed to create a PAM login because VT8 was already occupied and systemd marked the unit failed. That hook has been removed. The successful retest used the `TTYVHangup=yes` SIGHUP path. Boot-time startup and post-reboot RDP remain untested.

Files in `install/arm64/session/systemd/`:

- `omarchy-pi-uwsm-session.service` starts Sierra's PAM-backed tty8 session and the tested `start-hyprland` UWSM wrapper. Its privileged `chvt 8` pre-start was exercised in the manual trial. `TTYVHangup=yes` provides the stop path: it sends SIGHUP to UWSM, which stopped the graphical target and dependent services with `Result=success` in the amended manual retest. The unit remains disabled pending the final baseline and reboot gates.
- `omarchy-pi-hypr-rdp.service` is wanted by and bound to `graphical-session.target`. It launches only after UWSM has imported a Wayland environment and the preflight accepts the pinned binary, private config, and socket.
- `verify-hypr-rdp-runtime.py` rejects a changed binary, non-loopback or changed profile, exposed/missing credentials, a stale Wayland socket, and incomplete, malformed, mismatched, or unexpected TLS files. The password path is pinned to the dedicated `password` file. It never prints the secret or private-key contents.

The systemd managers are separate: the system service owns the tty8 PAM session; the UWSM compositor and RDP server belong to Sierra's user manager. The tested tty hangup asks UWSM to stop its graphical target, which stops the bound RDP service. The user manager may remain alive because SSH is logged in; this does not justify enabling lingering. Do not start another Sierra graphical session during the trial.

## Persistent RDP identity and secrets

Use a dedicated, persistent RDP-only credential. Do not reuse Sierra's login/SSH password or the bounded-test `rdp-test` credential. The profile accepted by the preflight is:

```toml
bind = "127.0.0.1:3389"
username = "omarchy-pi"
password_file = "/home/sierra/.config/omarchy-pi-rdp/password"
resolution = "1280x720"
fps = 20
egfx_codec = "avc420"
audio_mode = "off"
file_transfer_mode = "off"
```

Keep this config and the separate password file in Sierra-owned `/home/sierra/.config/omarchy-pi-rdp/`, mode `0700`; both files are mode `0600`. Record the dedicated username/password in the trusted local `./credentials/credentials.toml` using its existing secret-handling convention. Transfer the password file over the strict-host-key SSH connection from a private workstation file. Do not put the value in a command, unit, environment variable, log, repository, or screenshot.

The unit pins the tested hypr-rdp v0.1.6 executable at `/home/sierra/.local/src/hypr-rdp-v0.1.6-build-20260923/target/release/hypr-rdp`, SHA-256 `b466c691ebf62378a8eaf5498bbd7bb487035732cd96ba3f1be94b67dcd780d1`, source commit `8744778de2eb9add74224d57fac399aba6039a26`. The RDP service binds only to loopback; access remains through an SSH tunnel.

The server automatically stores its TLS identity in `/home/sierra/.config/hypr-rdp/{cert.pem,key.pem}`. If the directory is absent, the first successful service start may create the pair and `.tls.lock`; an existing directory must contain a complete, nonempty, parseable, matching pair, with no unrecognized entries. The private directory is mode `0700`, the private key and optional zero-byte lock are mode `0600`. Preserve the complete pair across manual stop, client reconnect, and reboot so the server certificate remains stable. The preflight rejects a partial pair, symlink, wrong owner/mode on the key or lock, or leftover temporary generation file. Keep private-key content out of the recovery report; record hashes in private evidence.

## Gates before staging on the Pi

Wait for the fresh post-repair encrypted recovery baseline and independent protected-state audit to pass. Keep two independent SSH connections, the known-good host key, LUKS USB recovery key, and a physical console operator available. Do not change SSH, networking, firewall, boot, target, PAM, display-manager, package, or encryption state as part of this stage.

Recheck on the Pi immediately before staging:

- exact boot ID, package versions/reasons, account files, `/boot` inventory, LUKS metadata, USB-key hash, EEPROM, network routes/addresses, firewall rules, and protected paths match the fresh baseline;
- the pinned executable still has the recorded hash, version, owner, mode, and resolved libraries;
- the dedicated config/password files have the required owner and modes, and the config has exactly the accepted loopback/audio/file-transfer profile;
- the certificate directory is either absent or a Sierra-owned mode-`0700` directory with a complete pair; a partial pair, incomplete prior generation, unexplained TLS files, or changed identity is a stop;
- tty8 is not occupied, `getty@tty8.service` is inactive and disabled, no Sierra compositor/graphical target/Wayland socket is active, and the four established session variables plus `HYPRLAND_INSTANCE_SIGNATURE` are absent from the user manager;
- no local operator is using the VT that the service will switch away from. Record `sudo fgconsole` and keep the physical console clear for tty8.

If these checks differ, do not stage the units. The prior `glslang` file repair makes a fresh integrity check especially important; its cause is unknown.

## Stage without enabling boot startup

Use a new Sierra-owned `0700` directory under `~/.local/state/omarchy-pi-unit-stage.<run-id>`. Transfer these three files from this source tree through the verified SSH connection and record their SHA-256 values in the private run evidence:

```text
install/arm64/session/systemd/omarchy-pi-uwsm-session.service
install/arm64/session/systemd/omarchy-pi-hypr-rdp.service
install/arm64/session/systemd/verify-hypr-rdp-runtime.py
```

On the Pi, validate staging-directory ownership/mode, source hashes, script syntax, and both exact units with the Pi's own `systemd-analyze --system verify` and `systemd-analyze --user verify`. Inspect every diagnostic. In particular, verify the executable, preflight, config and user-unit paths exist before installation. Do not silence missing-command or dependency warnings.

Refuse any existing final path, including dangling symlinks:

```text
/etc/systemd/system/omarchy-pi-uwsm-session.service
/home/sierra/.config/systemd/user/omarchy-pi-hypr-rdp.service
/usr/local/libexec/omarchy-pi/verify-hypr-rdp-runtime.py
```

Check `/usr/local/libexec/omarchy-pi` is either absent or a real root-owned mode-`0755` directory. Install the preflight root:root mode `0755`, system unit root:root mode `0644`, and user unit Sierra:Sierra mode `0644`; compare each installed file byte-for-byte with its staged source. Run both Pi-native `systemd-analyze verify` commands again against the installed paths, then reload the system and user managers. Enable **only** the user RDP unit for `graphical-session.target`; keep the system tty8 unit installed but disabled. Confirm the user RDP unit stays inactive and port `3389` is not listening in the SSH-only state.

## Manual service start/stop trial

Keep the second SSH session open. Capture the current foreground VT. With no existing seat0 session, tty8 user, compositor, graphical target, Wayland socket, or active graphical unit, start the system service manually while it is disabled:

```sh
sudo systemctl start omarchy-pi-uwsm-session.service
```

The unit will switch the foreground to VT8 before opening the controlling tty and PAM session. Verify from SSH and the physical console that:

1. The unit remains active and owns one local Sierra seat0 PAM session on VT8; the system journal has no start failure.
2. `wayland-wm@start\x2dhyprland.service` has the expected `start-hyprland` MainPID and exactly one direct Hyprland child; UWSM's graphical-session target is active.
3. The RDP user unit starts with Sierra's live Wayland socket, `verify-hypr-rdp-runtime.py` reports success, and exactly one listener exists at `127.0.0.1:3389`.
4. Using the accepted FreeRDP-over-SSH-tunnel harness, capture the actual client window and prove live frames, a harmless keyboard action, an observable pointer action, client close, and reconnect to the same Hyprland and hypr-rdp processes. Do not count a Pi-side `grim` image as client proof.
5. The first start creates a complete, Sierra-owned TLS certificate/key pair with the key mode `0600`. Record both hashes in private evidence. Do not remove the pair or `.tls.lock` during cleanup.

Then stop the system service. `TTYVHangup=yes` sends SIGHUP to the tty8 clients; the tested UWSM response stops the graphical target and the RDP service bound to it:

```sh
sudo systemctl stop omarchy-pi-uwsm-session.service
```

Require the system service to stop with `Result=success`; inspect the journal to confirm the SIGHUP-driven UWSM shutdown completed without PAM or unit errors. Require the RDP service, compositor unit and graphical-session target to be inactive; the checked wrapper and Hyprland PIDs to be gone; port 3389 and the RDP-created output to be gone; the tty8 PAM session and Wayland socket to be gone; and `WAYLAND_DISPLAY`, `XDG_CURRENT_DESKTOP`, `OMARCHY_PATH`, `OMARCHY_PI_MINIMAL_SESSION`, and `HYPRLAND_INSTANCE_SIGNATURE` to be absent from the user manager. The first stop attempt removed the session resources but failed its unit result because the then-installed `ExecStop` helper raced shutdown and hit `VirtualTerminalAlreadyTaken`. The amended retest passed this gate: the unit returned `Result=success`, the graphical/session resources were gone, tty1 was restored, and the TLS hashes matched. The service leaves the foreground VT at 8; only after confirming the console is clear, restore the recorded VT manually. Confirm fresh SSH still works and that the system/user managers, package state, network, and protected files match the immediately preceding trial baseline. Verify the same TLS hashes remain on disk.

If the updated stop fails or leaves any session resource active, keep the unit disabled and inspect from the second SSH session. Do not kill a PID, user manager, or Sierra session by name. Check the system journal, exact UWSM unit state and recorded process identities, then use the existing session runbook's targeted cleanup. Do not enable at boot until this manual lifecycle can start and stop cleanly.

## Enable and controlled reboot gate

After the manual lifecycle, actual-client reconnect, certificate preservation, and rollback checks all pass, take a new encrypted recovery baseline containing the installed units, preflight, config, credential, certificate state, and their checksums. Independently verify decryptability and compare all protected state. Keep two SSH routes and the physical console operator available for this one supervised reboot.

Enable only the system service, after verifying the tty8 getty conflict is still clear:

```sh
sudo systemctl enable omarchy-pi-uwsm-session.service
systemctl --user is-enabled omarchy-pi-hypr-rdp.service
sudo systemctl is-enabled omarchy-pi-uwsm-session.service
```

Reboot only after a final baseline comparison and physical readiness check. After it returns, require a changed boot ID, encrypted NVMe root and `/boot`, the same boot/encryption/EEPROM state, fresh SSH access, the intended local Sierra tty8 session, expected UWSM wrapper/Hyprland processes, a healthy user RDP unit and loopback-only listener. Repeat FreeRDP live-view, keyboard, pointer, disconnect/reconnect proof through a fresh SSH tunnel. Compare TLS certificate and key hashes with the manual-trial values. A previous unattended encrypted reboot succeeded before these units existed; it does not count as this boot-time proof.

Do not proceed to a rolling package update as part of this gate. First record and review this boot and RDP result; maintenance has its own complete-transaction and preservation checks.

## Rollback

Use the second SSH route or physical recovery console. Stop the system unit through the tested `TTYVHangup`/UWSM shutdown path and restore the saved foreground VT after confirming the console is not in use:

```sh
sudo systemctl stop omarchy-pi-uwsm-session.service
```

Verify the graphical and PAM cleanup gates above. Disable the system service if it was enabled and disable the RDP user unit. Check that their enablement symlinks are gone. Compare each installed file with the retained staged copy before removing only these exact files; retain unrelated units, the dedicated RDP config/password, and the stable TLS pair until the post-rollback baseline comparison passes. Then reload both managers and confirm both units are disabled/not found, no tty8 Sierra session or compositor/socket/output/listener remains, SSH and networking work, and protected state matches the rollback baseline. If the hangup path leaves resources active, keep the second SSH connection and recover through the exact UWSM unit/session identities in the session runbook. Never use broad `pkill`, `loginctl terminate-user`, `systemctl --user exit`, default-target changes, or whole-directory removal. Keep the private credential and TLS material until their separately authorized cleanup; filesystem deletion is not secure erasure.

If the controlled reboot does not return, recover from the physical console/USB LUKS route, disable the named system unit and reboot only after restoring SSH/network and protected state. Do not restore a whole filesystem archive onto a running Pi.

## Evidence boundary

This proposal is based on the passing manual RDP run, the previously tested UWSM wrapper session, Pi-native unit validation, and a successful amended manual lifecycle. The initial stop attempt cleaned up the session but failed its system unit result because the obsolete `ExecStop` helper raced the `TTYVHangup`-driven shutdown and could not create a second PAM login on tty8. The source and Pi helper were removed; the repeated start/stop passed with `Result=success`, and TLS identity matched. Automatic tty startup and post-reboot RDP remain untested.
