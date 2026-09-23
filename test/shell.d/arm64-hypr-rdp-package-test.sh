#!/bin/bash

set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

pkgbuild="$ROOT/install/arm64/packages/hypr-rdp/PKGBUILD"

[[ -f $pkgbuild ]] || fail "aarch64 hypr-rdp PKGBUILD exists"
pass "aarch64 hypr-rdp PKGBUILD exists"

bash -n "$pkgbuild" || fail "hypr-rdp PKGBUILD has valid Bash syntax"
pass "hypr-rdp PKGBUILD has valid Bash syntax"

grep -Fqx 'pkgname=hypr-rdp' "$pkgbuild" || fail "package keeps upstream executable package name"
grep -Fqx "pkgver=0.1.6" "$pkgbuild" || fail "package pins the accepted hypr-rdp release"
grep -Fqx "arch=('aarch64')" "$pkgbuild" || fail "package is limited to the tested target architecture"
grep -Fq '_hypr_rdp_commit=8744778de2eb9add74224d57fac399aba6039a26' "$pkgbuild" || fail "source is pinned to the reviewed upstream commit"
grep -Fqx "sha256sums=('5890af2c22e45994354f90ddf970ca7e4668dbdecb7553c48f0b7822d89a2cc7')" "$pkgbuild" || fail "upstream source archive has a pinned SHA-256"
pass "version, architecture, source commit, and source checksum are pinned"

grep -Fq "depends=('gcc-libs' 'glibc' 'libpipewire' 'libxkbcommon' 'zlib')" "$pkgbuild" || fail "runtime dependency list matches the tested binary's shared libraries"
grep -Fq "makedepends=('clang' 'cmake' 'fakeroot' 'gcc' 'git' 'make' 'pkgconf' 'rust')" "$pkgbuild" || fail "build dependency list covers native Rust and C build requirements"
grep -Fqx "options=('!strip')" "$pkgbuild" || fail "makepkg must not change the executable after its digest is written"
pass "runtime and source-build dependencies are declared"

grep -Fq 'cargo fetch --locked' "$pkgbuild" || fail "Cargo fetch honors the committed lockfile"
grep -Fq 'cargo build --release --locked --offline --no-default-features --jobs 2' "$pkgbuild" || fail "build uses the accepted locked offline feature profile"
pass "build resolves from Cargo.lock and compiles without network access"

stage_dir=$(mktemp -d)
trap 'rm -rf "$stage_dir"' EXIT
# makepkg defines srcdir and pkgdir after loading PKGBUILD. Reproduce that
# order so a source-time path calculation cannot silently point at /.
unset srcdir pkgdir
source "$pkgbuild"
export srcdir="$stage_dir/src"
export pkgdir="$stage_dir/pkg"
source_dir="$srcdir/hypr-rdp-8744778de2eb9add74224d57fac399aba6039a26"
mkdir -p "$source_dir" "$stage_dir/bin"
printf 'test license\n' > "$source_dir/LICENSE"
cat > "$stage_dir/bin/cargo" <<'SH'
#!/bin/bash
printf '%s\t%s\t%s\n' "$PWD" "${CARGO_HOME:-}" "$*" >> "$PKG_TEST_CARGO_LOG"
if [[ $1 == build ]]; then
  mkdir -p target/release
  printf 'test executable payload\n' > target/release/hypr-rdp
  chmod 755 target/release/hypr-rdp
fi
SH
chmod 755 "$stage_dir/bin/cargo"
export PKG_TEST_CARGO_LOG="$stage_dir/cargo.log"
PATH="$stage_dir/bin:$PATH" prepare
PATH="$stage_dir/bin:$PATH" build
[[ $(wc -l < "$PKG_TEST_CARGO_LOG") == 2 ]] || fail "prepare and build each invoke Cargo once"
grep -Fq "$source_dir" "$PKG_TEST_CARGO_LOG" || fail "Cargo runs inside extracted pinned source"
grep -Fq "$srcdir/cargo-home" "$PKG_TEST_CARGO_LOG" || fail "Cargo uses source-local dependency cache"
package

binary="$pkgdir/usr/bin/hypr-rdp"
manifest="$pkgdir/usr/share/omarchy-pi/hypr-rdp.sha256"
[[ -x $binary ]] || fail "package stage installs an executable at /usr/bin/hypr-rdp"
[[ -f $manifest ]] || fail "package stage installs its checksum manifest"

expected_digest=$(sha256sum "$binary" | cut -d ' ' -f1)
manifest_digest=$(<"$manifest")
[[ $manifest_digest =~ ^[0-9a-f]{64}$ ]] || fail "checksum manifest contains one lowercase SHA-256 value"
[[ $manifest_digest == "$expected_digest" ]] || fail "checksum manifest matches the packaged executable"
[[ $(wc -l < "$manifest") == 1 ]] || fail "checksum manifest contains exactly one line"
pass "package stage installs the stable executable path and matching per-build digest"
