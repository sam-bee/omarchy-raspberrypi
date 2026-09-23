# Native hypr-rdp build on the Pi — 23 September 2026

The first RDP candidate built successfully on the Raspberry Pi 5 after the reviewed [build-tool transaction](build-tools-result.md). This is build evidence only; an RDP login, rendered client image, input, reconnect, persistent service and post-reboot session have not yet been proven.

The source was fetched over HTTPS at the `v0.1.6` tag and verified clean at commit `8744778de2eb9add74224d57fac399aba6039a26`. The Pi ran `cargo build --release --locked --no-default-features --jobs 2` in `/home/sierra/.local/src/hypr-rdp-v0.1.6-build-20260923`. Cargo exited 0 after 15m39s. Its peak sampled process-tree RSS was about 1.31 GiB, within the Pi's 8 GiB RAM. The build emitted eight nonfatal warnings. The private build log is `/home/sierra/.local/state/hypr-rdp-build-20260923/build-attempt-2.log` (mode 0600).

The resulting aarch64 executable is `/home/sierra/.local/src/hypr-rdp-v0.1.6-build-20260923/target/release/hypr-rdp`, SHA-256 `b466c691ebf62378a8eaf5498bbd7bb487035732cd96ba3f1be94b67dcd780d1`. `--version` returned `0.1.6`; `--help` completed and included the expected config, audio and file-transfer options. `ldd` found no unresolved dynamic libraries. The binary dynamically links to libxkbcommon, libstdc++, libpipewire-0.3, zlib, libgcc_s, libm and libc.

The first build wrapper exited before running Cargo because `/usr/bin/time` was not installed. The successful retry used Bash's built-in timing. The build did not launch `hypr-rdp`; the post-build check found no server process or listener on TCP 3389.

Next, verify a fresh post-build encrypted recovery baseline, then use the [bounded RDP test](README.md) with a real FreeRDP client. The only accepted positive result is a captured client view with visible frame changes, remote keyboard and pointer input, and a disconnect/reconnect cycle. The earlier `grim` image was captured from the Pi's Wayland output and is not RDP evidence.
