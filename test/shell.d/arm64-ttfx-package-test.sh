#!/bin/bash

set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

pkgbuild="$ROOT/install/arm64/packages/ttfx/PKGBUILD"
readme="$ROOT/install/arm64/packages/ttfx/README.md"
policy="$ROOT/install/arm64/packages.tsv"

[[ -f "$pkgbuild" ]] || fail "ARM64 ttfx PKGBUILD exists"
[[ -f "$readme" ]] || fail "ARM64 ttfx build guidance exists"

grep -Fqx "pkgver=0.3.2" "$pkgbuild" || fail "ttfx recipe pins v0.3.2"
grep -Fqx "arch=('aarch64')" "$pkgbuild" || fail "ttfx recipe is ARM64-only"
grep -Fq 'cargo fetch --locked' "$pkgbuild" || fail "ttfx recipe fetches the locked dependency set"
grep -Fq 'cargo build --frozen --release' "$pkgbuild" || fail "ttfx recipe builds the frozen dependency set"
grep -Fqx "sha256sums=('d0c0df4867e7f03142fb7f77c66670d0e8da15534239c1a7abfd89f19dfc00f6')" "$pkgbuild" || \
  fail "ttfx recipe pins the reviewed source archive"
grep -Fq '49e2091962fc4d425b4cf3bde1a105719b5b50eed0583ec90e85922adb45e2ce' "$readme" || \
  fail "ttfx guidance records the reviewed Cargo.lock hash"

row=$(awk -F '\t' '$1 == "ttfx" { print; found = 1 } END { if (!found) exit 1 }' "$policy") || \
  fail "ttfx has an ARM64 package policy row"
[[ $(awk -F '\t' '{ print NF }' <<<"$row") == 4 ]] || fail "ttfx policy row has the four expected columns"
[[ $(cut -f2 <<<"$row") == candidate ]] || fail "ttfx policy row is a candidate"

pass "ARM64 ttfx recipe and candidate policy stay pinned"
