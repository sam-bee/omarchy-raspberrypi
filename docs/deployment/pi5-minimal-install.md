# Raspberry Pi 5 minimal session: installation and acceptance

This guide describes the first repeatable deployment target for the Pi profile: an already booting Arch Linux ARM Raspberry Pi 5 with an administrator account, working network and SSH, and a recovery route. The profile installs Omarchy's minimal Hyprland, Quickshell and Foot session, with optional loopback-only RDP. The underlying session has been exercised on one 8 GB Pi 5 with encrypted NVMe; the account-independent deployment here has not yet been installed there or independently reproduced from a clean base. A package maintenance cycle remains a separate acceptance gate. The historical package and unit runbooks record the exact transactions on that machine, not package versions to copy into a new installation.

## Base-system contract

Record the target's architecture, Pi model, root and `/boot` mounts, bootloader/firmware, kernel, initramfs hooks, network and SSH state, user name/UID/home, and a recovery route before changing it. The Pi-native boot chain and Arch Linux ARM repository and keyring remain authoritative. If using the existing encrypted NVMe setup, preserve its LUKS keyslots and USB unlock key. The minimal profile does not provision disks, encryption, networking or accounts.

Use a spare Pi or designated spare boot storage for the independent installation. Keep the working domestic Pi available while rehearsing this path. A clean-base rehearsal needs its own signed package resolution and a backup verified outside the target; the [first graphical package transaction](first-session/README.md), [minimal-session package stage](minimal-session/package-stage.md) and [RDP build-tool result](remote-desktop/build-tools-result.md) are worked examples of the package and hook review.

## Desktop packages

The known session roots are `hyprland`, `mesa`, `foot`, `ttf-jetbrains-mono-nerd`, `uwsm`, `quickshell`, `xdg-terminal-exec`, `inotify-tools`, `qt6-wayland` and `grim`. The source checkout and staging script also need `git`, `tar`, Python 3 and `flock`; session startup uses systemd, D-Bus and Hyprland tools. Resolve these against fresh target repository metadata and compare the **complete** proposed transaction, including upgrades, replacements, package scripts and hooks, with the protected base. Verify every selected archive signature using the target's trusted keyring. Keep the package manifest, install reasons and signed archives in the private recovery record. Apply a reviewed complete transaction, then recheck boot, network, SSH and package state before session configuration.

The existing [ARM planner](../arm64.md) classifies the full upstream base manifest and always reports `ready_to_apply: false`. It is a planning aid; the ten roots above describe the smaller session already demonstrated on the Pi.

## Source and user session

Clone the `quattro-rpi5` implementation under the chosen desktop user's home and record its exact revision. Use read access to that repository if it requires authentication. From the resulting clean checkout, stage the session as that user:

```bash
mkdir -p "$HOME/.local/src"
git clone --branch quattro-rpi5 --single-branch https://github.com/sam-bee/omarchy-raspberrypi.git "$HOME/.local/src/omarchy-raspberrypi"
cd "$HOME/.local/src/omarchy-raspberrypi"
git rev-parse HEAD
git status --porcelain --untracked-files=all
bash install/arm64/stage-user-session.sh
readlink "$HOME/.local/share/omarchy-pi/current"
Hyprland --verify-config --config "$HOME/.config/hypr/hyprland.lua"
xdg-terminal-exec --print-id
```

The stager archives the committed tree under `~/.local/share/omarchy-pi/releases/` and publishes the four user-owned configuration files only after checking for existing destinations. It refuses a dirty checkout, an existing unmanaged file on first install, and an attempted repeat of the current revision. For a newer committed revision, stop the selected graphical session, run the same command from its clean checkout, verify the new pointer and configurations, then start the session again. It updates only files that still match the former release and preserves edits or missing files. To return to the immediately previous revision, stop the session, run `bash install/arm64/rollback-user-session.sh` from the checkout, verify the pointer and configuration, then start the session. The stager records and recovers interrupted switches; if it reports a conflict, inspect its transaction record before retrying.

## Persistent session unit

Install the system unit and its launcher from the same recorded revision. Check that the destination paths are absent before the first install; review existing files before any later replacement. The instance name selects the login account, while the launcher obtains that account's home and UID from the account database. Run these commands on the Pi from the repository root:

```bash
sudo install -Dm644 install/arm64/session/systemd/omarchy-pi-uwsm-session@.service /etc/systemd/system/omarchy-pi-uwsm-session@.service
sudo install -Dm755 install/arm64/session/systemd/start-uwsm-session.sh /usr/local/libexec/omarchy-pi/start-uwsm-session.sh
sudo systemd-analyze verify /etc/systemd/system/omarchy-pi-uwsm-session@.service
sudo systemctl daemon-reload
session_unit=$(systemd-escape --template=omarchy-pi-uwsm-session@.service -- "$(id -un)")
sudo systemctl start "$session_unit"
```

Keep a separate working SSH connection while testing tty8. Require a local logind session for the chosen user, the V3D renderer, a visible Foot window and a clean `hyprctl configerrors`. Stop the same instance and confirm UWSM, Hyprland and the tty8 login exit cleanly before enabling it at boot. Then enable the instance, make a controlled reboot with recovery access available, and repeat the session and SSH checks. The [previous manual lifecycle](minimal-session/persistent-startup-draft.md) records the detailed checks used on the original Pi.

## Optional RDP package and user unit

The [hypr-rdp package recipe](../../install/arm64/packages/hypr-rdp/PKGBUILD) pins the v0.1.6 source commit and archive digest and builds with Cargo's locked dependencies. Its build requirements include Rust, Clang, CMake, a C compiler, Make, pkgconf, Git and fakeroot. Review and install any missing build dependencies through a complete signed target transaction. Copy the recipe to a private scratch directory outside the Git checkout, then build it there:

```bash
mkdir -p "$HOME/.cache"
build_dir=$(mktemp -d "$HOME/.cache/omarchy-pi-hypr-rdp.XXXXXXXX")
install -m644 install/arm64/packages/hypr-rdp/PKGBUILD "$build_dir/PKGBUILD"
( cd "$build_dir" && makepkg --verifysource && makepkg --noconfirm )
```

Inspect the produced package and its metadata before installing it under the target's local-package signature policy. After confirming it contains only the expected executable, license and digest manifest, and that the package transaction has no unexpected effects, use the exact file reported by `makepkg --packagelist`:

```bash
package_file=$(cd "$build_dir" && makepkg --packagelist)
pacman -Qip "$package_file"
pacman -Qlp "$package_file"
sudo pacman -U -- "$package_file"
sudo pacman -Qkk hypr-rdp
```

This keeps build outputs out of the clean checkout required by session staging. The package installs `/usr/bin/hypr-rdp` and a package-owned digest at `/usr/share/omarchy-pi/hypr-rdp.sha256`. The digest describes that build; binary bytes can differ across toolchains.

As the desktop user, run `python3 install/arm64/setup-rdp-credentials.py` from the same checkout in an interactive terminal. It asks twice for a separate RDP password and creates `~/.config/omarchy-pi-rdp/{config.toml,password}` with private permissions. Preserve the generated TLS pair under `~/.config/hypr-rdp/` across restarts and upgrades. Keep port 3389 bound to loopback and reach it through an SSH local forward.

Install the user unit and preflight helper after the package and private profile pass inspection:

```bash
sudo install -Dm644 install/arm64/session/systemd/omarchy-pi-hypr-rdp.service /etc/systemd/user/omarchy-pi-hypr-rdp.service
sudo install -Dm755 install/arm64/session/systemd/verify-hypr-rdp-runtime.py /usr/local/libexec/omarchy-pi/verify-hypr-rdp-runtime.py
systemd-analyze --user verify /etc/systemd/user/omarchy-pi-hypr-rdp.service
systemctl --user daemon-reload
systemctl --user enable omarchy-pi-hypr-rdp.service
```

Start the unit only when the chosen user's graphical session and Wayland socket exist. Require the preflight to pass, confirm `ss -ltn` has only a `127.0.0.1:3389` RDP listener, then test real client view, input and reconnect through the tunnel. Stop and restart the graphical session to check that RDP follows it. Preserve the certificate and key when stopping the server. The [RDP acceptance runbook](remote-desktop/README.md) gives the client and TLS checks used on the original Pi.

## Acceptance record

For a new installation, record the package transaction, source revision, user name/UID/home, unit and executable versions, and changes to protected state. Prove a local session with the Pi V3D renderer, one output, the Omarchy bar, a mapped Foot window and no Hyprland configuration errors. If RDP is installed, require only `127.0.0.1:3389` to listen and use a real client over SSH to check visible frames, keyboard, pointer and reconnect. Then stop and restart the session, make a controlled reboot, and check encrypted root (where configured), `/boot`, network, fresh SSH, session startup, RDP and stable TLS identity. The [existing Pi's result](minimal-session/persistent-startup-result.md) is the reference for these checks; record a separate result for the clean-base installation.

Upgrade acceptance requires a second committed source revision, a user-edited configuration file, and a rollback to the previous revision. Check that user edits survive both operations and that the working session returns after restart. A complete signed package update and reboot, when candidates exist, is a separate gate under the [maintenance runbook](maintenance/README.md).
