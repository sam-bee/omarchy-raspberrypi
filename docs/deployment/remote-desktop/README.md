# Hyprland RDP source-build and acceptance plan

**Status: manual RDP acceptance is a bounded experiment.** Complete and record the build, protected-state, and real-client gates below before considering any persistent deployment. Keep machine-specific logs, screenshots, hashes, package inventories, and acceptance records in a private evidence store outside this repository. A successful manual test does not establish persistent startup, RDP after reboot, or a maintenance update.

## Scope and prerequisites

The candidate used for this procedure is [hypr-rdp v0.1.6](https://github.com/MuNeNICK/hypr-rdp/releases/tag/v0.1.6), built natively from its pinned source. Its declared Hyprland minimum is 0.54; confirm that the target compositor meets that requirement. The acceptance record should cover capture, login, input, and reconnect evidence for the target system.

Keep this experiment to one manually started process inside the existing logged-in Hyprland session. Do not create a system service, enable user lingering, change login behavior, expose a listener to the LAN, install a portal, or change boot, storage, encryption, network, or firewall configuration. RDP reachability does not prove unattended encrypted boot; it depends on a usable graphical user session already existing.

Before any package transaction, follow the current backup and protected-state checks in the [maintenance gate](../maintenance/README.md) and [first-session commands](../first-session/commands.md). Retake the live package and service inventory, confirm at least 2 GiB free on root, preserve the existing SSH connection, and keep the operator's physical recovery route available. Do not proceed if a protected baseline or the active session has drifted. Do not use `pacman -Sy` on the live target.

The server must run as the logged-in user with the real `WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR`, and Hyprland instance environment. Stop if there is no logged-in Hyprland session or if the server cannot attach to it. A systemd user service or another unattended startup arrangement is a later, separately verified stage.

Leave the existing primary headless output unchanged. With the `output` option unset, v0.1.6 creates a separate headless monitor for the RDP session and applies the requested size to that monitor; the test example uses 1280x720 and the monitor is normally named with the `hypr-rdp` prefix. Other runs may receive a suffixed name. Before launch, record `hyprctl -j monitors` and stop if an unexplained output with that prefix already exists, because v0.1.6 may adopt a stale output and remove it when its guard drops. After disconnect and server exit, confirm only the RDP-created monitor disappeared.

## Candidate and build profile

Use a clean source checkout at the upstream v0.1.6 tag and retain its source and build logs outside this repository. The release and AUR package are x86_64-only; an ARM target needs a native aarch64 build. The source pins the IronRDP git dependency and Cargo.lock dependencies, so require `--locked` and record the source commit and checksum of the resulting binary in private evidence.

Build the lean software-encoding profile:

```sh
cargo build --release --locked --no-default-features
```

In v0.1.6, the default features are `vaapi` and `client-to-server`. `--no-default-features` omits the VA-API encoder and client-to-server file transfer support. The remaining compile-time default can still allow server-to-client file transfer, so set `file_transfer_mode = "off"` explicitly to disable both directions. The build retains the bundled OpenH264 software encoder, H.264/AVC420, capture, keyboard and pointer input, TLS, and text/image clipboard support. PipeWire remains an unconditional crate dependency; set `audio_mode = "off"` for this test so it does not try to set up audio or require `pactl`. Confirm that the target has the required `libpipewire` library and headers; the PipeWire daemon package is not needed for audio-off operation.

The source requires both virtual-input protocols used by its input path: `zwp_virtual_keyboard_manager_v1` and `zwlr_virtual_pointer_manager_v1`. If either protocol is missing or Hyprland rejects its bind, stop and record the error; do not relax compositor security or add a new input backend during this test.

## Preliminary aarch64 build-dependency preview

The following is a **preliminary read-only resolution**, not an approved install manifest. Resolve the build-tool closure against current reviewed metadata, without changing package state. Fresh metadata, exact archives, signatures, transaction effects, free space, and package state must be rechecked before any install. Do not use a previous snapshot as a transaction authorization.

| Package | Version to review | Build purpose |
| --- | --- | --- |
| `rust` | current reviewed version | Rust compiler and Cargo |
| `clang` | current reviewed version | libclang for PipeWire's unconditional bindgen build |
| `cmake` | current reviewed version | Native AWS-LC build used for RSA TLS certificate generation |
| `make` | current reviewed version | CMake's Unix Makefiles generator |
| `pkgconf` | current reviewed version | PipeWire and system library discovery |
| dependencies | resolve from current metadata | Compiler, linker, CMake, libgit2, and build support |

Confirm the target already has the runtime libraries required by the selected profile and determine which build tools are absent before resolving the transaction. The missing optional runtime libraries are why the no-default-feature/audio-off profile is preferred. OpenH264's NASM path applies to x86/x86_64; its build logic does not require NASM for aarch64.

Before authorizing a build-dependency transaction, create and encrypt a fresh recovery baseline, resolve the complete closure against reviewed metadata, inspect archives and hooks, and verify signatures as described by the [maintenance gate](../maintenance/README.md). These build tools consume close to 1 GiB installed before Cargo's own source/build cache, so review available disk space and expected build time. After a successful experiment, retain the exact package delta; do not run a broad recursive package removal as automatic cleanup.

## Isolated configuration and server start

Keep the test configuration and RDP password in a private temporary directory created by the operator, mode `0700`. Store the password in a separate file with mode `0600`; do not put it in the command line, shell history, logs, repository, synced workspace, or a world-readable config. Use a dedicated nonempty RDP username and password, both set. The server treats a missing half as an empty string. The credentials are RDP application credentials; do not reuse the account's SSH or login password.

Use the flat TOML keys supported by v0.1.6, with values equivalent to:

```toml
bind = "127.0.0.1:3389"
username = "rdp-test"
password_file = "/private/test/directory/password"
resolution = "1280x720"
fps = 20
egfx_codec = "avc420"
audio_mode = "off"
file_transfer_mode = "off"
```

The example path is a placeholder and must be replaced with the private test directory. Check for existing `~/.config/hypr-rdp/cert.pem` and `key.pem` before starting. If both exist, record and preserve them; v0.1.6 reuses them without checking their permissions. If exactly one exists, stop and review rather than allowing startup to replace the incomplete pair. If neither exists, the server generates a self-signed RSA certificate under that directory and creates its private key as mode `0600`; verify the resulting key and directory permissions. The certificate names `localhost` and `127.0.0.1`.

From the active Hyprland session, build from the tagged checkout, inspect `ldd` for unresolved libraries, verify the binary hash and `--help`, and start the resulting `target/release/hypr-rdp` in the foreground with the private config file. Do not enable a service or pass the RDP password with `-p`. Confirm that the process reports a managed headless output and that `ss -ltn` shows the listener only at `127.0.0.1:3389`. If the server binds another address, stop it immediately.

## Actual RDP client test and acceptance gates

Use an actual FreeRDP X11 client on a separate workstation. Check the client build's codec support before testing; hypr-rdp v0.1.6 has a non-H.264 path. When the client opens EGFX but advertises no AVC support, the server may use EGFX ClearCodec; if EGFX does not activate, it may send ordinary RDP bitmap updates after a bounded activation wait. There is no `egfx_codec` value that explicitly selects ClearCodec; accepted values are `auto`, `avc420`, and `avc444`, and `auto` is not a non-H.264 selector.

For reproducible, headless client evidence, run the X11 client inside a separate Xvfb display and use `xdotool` for input and `xwd` or an equivalent capture tool for the client window. These workstation packages also require their own review; they are not target dependencies. The remote end must be reached only through an SSH local forward, for example `127.0.0.1:13389` on the workstation to `127.0.0.1:3389` on the target. Require `ExitOnForwardFailure` and confirm the tunnel is established before launching FreeRDP.

Use FreeRDP's certificate TOFU/pinning mode for the first connection; do not use a certificate-ignore option. Pass the RDP username as required by the client and supply the password through the client's protected stdin or a mode-`0600` client profile supported by that exact version. Do not put a password in process arguments or logs. Record the client version and build configuration, selected certificate fingerprint, server log, and tunnel endpoint. Keep screenshots private and redact personal or sensitive desktop content before sharing.

The RDP test passes only when all of the following are observed and recorded:

1. The FreeRDP window shows the live Hyprland session through the SSH tunnel, including a recognizable state that differs from the workstation desktop. A screenshot from `grim`, `hyprctl`, or an SSH file copy is not sufficient.
2. The client can click a known target and type a harmless marker in a visible application in the remote session. The visible response proves input reached the compositor. Do not run unreviewed commands or type secrets into the remote desktop.
3. The cursor visibly moves and a pointer click has an observable effect in the session.
4. Close the FreeRDP client, retain the server process and SSH tunnel, then connect again. The second RDP connection shows the same target session and accepts input. Record any expected first-client displacement; v0.1.6 permits an authenticated connection to replace the current one.
5. `ss -ltn` still shows only the loopback listener; `hyprctl -j monitors` shows a separate 1280x720 `hypr-rdp` output while connected, alongside the unchanged 1280x720 `omarchy-pi` output. The server log identifies the usable codec path. Recheck the monitor list after disconnect and confirm only the RDP-created output was removed.

If the client cannot verify/pin the certificate, cannot authenticate, shows no live session or advancing frames, fails either input test, or the server logs no usable AVC/ClearCodec/bitmap path, stop and record the exact client/server logs. Do not count a partial connection or a static first frame as proof of remote control. Do not expose port 3389 to the LAN to troubleshoot it.

## Cleanup and protected-state checks

After recording the outcome, stop only the named foreground `hypr-rdp` process, the FreeRDP client, Xvfb, and the SSH tunnel created for this experiment. Do not kill the logged-in user's user manager or unrelated sessions. If the temporary config/password directory was created only for this test, remove it after the server stops and verify that no test password remains in shell history, process listings, or logs. Remove only a certificate/key pair created by this test; preserve any pre-existing pair and record its fingerprint. Remove the client TOFU entry created for this test only if it is no longer wanted, using the chosen client's documented cache procedure.

Capture the final package inventory and install reasons, compare the package delta with the reviewed build closure, and inspect the pacman log. Confirm there was no unrelated package change, new enabled service, changed default target, active listener on a non-loopback address, firewall/routing change, or alteration to `/boot`, kernel/initramfs, LUKS metadata/keyslots, EEPROM, SSH, Wi-Fi, pacman configuration, or existing protected paths. Compare their recorded hashes and absent paths with the fresh pretest baseline. Investigate every unexplained difference before rebooting or removing any package. The RDP experiment itself does not authorize a reboot or a maintenance update.

If the test installed build dependencies, do not silently leave an unexplained package delta or run `pacman -Rns` to clean it up. Review any removal as a separate exact transaction, preserve packages required elsewhere, and rerun protected-state checks afterward. Do not install or enable a persistent RDP service until the manual view/input/reconnect gate passes and a separate startup plan demonstrates that Hyprland and the server share the required user-session environment.

## Fallback: Lamco RDP Server

If the pinned hypr-rdp build fails on aarch64 or the required Wayland capture/input protocols fail on the installed Hyprland version, stop that candidate and preserve its logs. The fallback is Lamco RDP Server, whose current Hyprland guide describes direct `wlr-screencopy` capture and virtual keyboard/pointer input. A package listing aarch64 is not proof of compatibility on the target. Recheck current source, license, architecture metadata, transaction size, runtime dependencies, and hooks before any install.

Lamco's documented default listens on `[::]:3389` and has no authentication. For a fallback test, configure explicit loopback binding and authenticated access before launch, retain the SSH-only tunnel, and repeat the same view/input/reconnect gates. Do not use its default listener or treat its product compatibility matrix as Pi acceptance evidence. If Lamco also fails, leave RDP disabled and report the specific failed protocol/build gate for a separate design review.

## Sources and evidence handling

- [hypr-rdp v0.1.6 release](https://github.com/MuNeNICK/hypr-rdp/releases/tag/v0.1.6), [README](https://github.com/MuNeNICK/hypr-rdp/blob/v0.1.6/README.md), and [Cargo feature/dependency list](https://github.com/MuNeNICK/hypr-rdp/blob/v0.1.6/Cargo.toml) describe its declared requirements and build features.
- [TLS handling](https://github.com/MuNeNICK/hypr-rdp/blob/v0.1.6/src/server/tls.rs), [credential and bind parsing](https://github.com/MuNeNICK/hypr-rdp/blob/v0.1.6/src/config.rs), [server security-mode selection](https://github.com/MuNeNICK/hypr-rdp/blob/v0.1.6/src/server/mod.rs), and [Wayland input adapter](https://github.com/MuNeNICK/hypr-rdp/blob/v0.1.6/src/input/wayland.rs) are source review only, not a security audit or a completed login test.
- [PipeWire sys build script](https://docs.rs/crate/pipewire-sys/0.9.2/source/build.rs) confirms bindgen is run while building PipeWire bindings.
- [Headless output manager](https://github.com/MuNeNICK/hypr-rdp/blob/v0.1.6/src/capture/wayland/output.rs), [capture selection](https://github.com/MuNeNICK/hypr-rdp/blob/v0.1.6/src/capture/mod.rs), [EGFX fallback](https://github.com/MuNeNICK/hypr-rdp/blob/v0.1.6/src/egfx/factory.rs), [ClearCodec sender](https://github.com/MuNeNICK/hypr-rdp/blob/v0.1.6/src/egfx/rdpegfx.rs), and [bitmap fallback](https://github.com/MuNeNICK/hypr-rdp/blob/v0.1.6/src/capture/frame.rs) describe the source behavior. Record which capture and codec path the target actually exercises; the bitmap fallback requires separate validation.
- [Lamco Hyprland guide](https://lamco.ai/products/lamco-rdp-server/docs/v1/hyprland/) and [AUR package recipe](https://aur.archlinux.org/cgit/aur.git/plain/PKGBUILD?h=lamco-rdp-server) are fallback research references only.

Record whether the bounded manual RDP test proved live view, keyboard and pointer control, reconnect, loopback-only listening, and cleanup. Keep the resulting records and supporting artifacts in private evidence outside this repository. Persistent startup, RDP after reboot, a completed maintenance update, and the final independent protected-state audit remain open gates until separately verified.
