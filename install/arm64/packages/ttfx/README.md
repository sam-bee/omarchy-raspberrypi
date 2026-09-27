# ARM64 `ttfx` package

This recipe packages the first-party Omarchy screensaver renderer for the Pi's
`aarch64` target. It is pinned to upstream `omacom-io/ttfx` v0.3.2 (release
commit `7203e35`), including the terminal-loss fix used by the lock-screen
path.

The source archive is downloaded from:

`https://github.com/omacom-io/ttfx/archive/refs/tags/v0.3.2.tar.gz`

The recorded source SHA-256 is
`d0c0df4867e7f03142fb7f77c66670d0e8da15534239c1a7abfd89f19dfc00f6`.
The checked-in `Cargo.lock` SHA-256 is
`49e2091962fc4d425b4cf3bde1a105719b5b50eed0583ec90e85922adb45e2ce`.
The PKGBUILD uses `cargo fetch --locked` and `cargo build --frozen` so a
dependency update cannot be introduced by an unreviewed build.

On an ARM64 Arch target with the normal `base-devel` toolchain, verify and
build without installing the result:

```sh
makepkg --verifysource --noconfirm
makepkg --syncdeps --needed --noconfirm
pacman -Qip ./ttfx-0.3.2-1-aarch64.pkg.tar.*
```

The normal install is a separately reviewed local package transaction:

```sh
sudo pacman -U ./ttfx-0.3.2-1-aarch64.pkg.tar.*
```

The Pi-native review on 27 September 2026 used the existing Cargo cache and a
detached-signature-verified ARM `fakeroot` archive extracted under `/var/tmp`;
it did not install fakeroot or any build dependency system-wide. Genuine
`makepkg` produced `ttfx-0.3.2-1-aarch64.pkg.tar.xz` using the target's
`PKGEXT` setting. `pacman -Qip` confirmed `ttfx 0.3.2-1`, `aarch64`, runtime
dependencies `gcc-libs` and `glibc`, no install script, and only the binary,
documentation, license, and shell-completion files. The archive's entries and
mtree are root-owned; it is unsigned and still requires the separate reviewed
`pacman -U` step.
