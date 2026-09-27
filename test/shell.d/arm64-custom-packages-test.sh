#!/bin/bash

set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

builder="$ROOT/install/arm64/build-custom-packages.sh"
metadata="$ROOT/install/arm64/custom-packages.tsv"

[[ -x $builder ]] || fail "custom package builder is executable"
bash -n "$builder" || fail "custom package builder has valid Bash syntax"
[[ -f $metadata ]] || fail "custom package metadata exists"

row_count=$(awk -F '\t' '$1 !~ /^#/ && NF { count++ } END { print count + 0 }' "$metadata")
[[ $row_count == 2 ]] || fail "metadata lists exactly the two Pi custom packages"
grep -Fqx $'hypr-rdp\t0.1.6-1\t8744778de2eb9add74224d57fac399aba6039a26\t5890af2c22e45994354f90ddf970ca7e4668dbdecb7553c48f0b7822d89a2cc7' "$metadata" ||
  fail "hypr-rdp metadata pins version, source revision, and source checksum"
grep -Fqx $'ttfx\t0.3.2-1\t7203e35\td0c0df4867e7f03142fb7f77c66670d0e8da15534239c1a7abfd89f19dfc00f6' "$metadata" ||
  fail "ttfx metadata pins version, source revision, and source checksum"
pass "custom package metadata is pinned"

fixture=$(mktemp -d)
trap 'rm -rf -- "$fixture"' EXIT
hypr_archive="$fixture/hypr-rdp-0.1.6-1-aarch64.pkg.tar.zst"
ttfx_archive="$fixture/ttfx-0.3.2-1-aarch64.pkg.tar.zst"
printf 'test hypr-rdp archive\n' > "$hypr_archive"
printf 'test ttfx archive\n' > "$ttfx_archive"
hypr_sha=$(sha256sum "$hypr_archive" | awk '{print $1}')
ttfx_sha=$(sha256sum "$ttfx_archive" | awk '{print $1}')
cat > "$fixture/manifest.tsv" <<EOF
# schema_version=1
# columns=package version architecture source_revision source_sha256 package_sha256 package_signature filename
hypr-rdp	0.1.6-1	aarch64	8744778de2eb9add74224d57fac399aba6039a26	5890af2c22e45994354f90ddf970ca7e4668dbdecb7553c48f0b7822d89a2cc7	$hypr_sha	unsigned	$(basename -- "$hypr_archive")
ttfx	0.3.2-1	aarch64	7203e35	d0c0df4867e7f03142fb7f77c66670d0e8da15534239c1a7abfd89f19dfc00f6	$ttfx_sha	unsigned	$(basename -- "$ttfx_archive")
EOF
{
  printf '%s  %s\n' "$hypr_sha" "$(basename -- "$hypr_archive")"
  printf '%s  %s\n' "$ttfx_sha" "$(basename -- "$ttfx_archive")"
} > "$fixture/SHA256SUMS"
cat > "$fixture/manifest.json" <<EOF
{
  "schema_version": 1,
  "package_signatures": "unsigned",
  "archives": [
    {"name":"$(basename -- "$hypr_archive")","package":"hypr-rdp","version":"0.1.6-1","architecture":"aarch64","source_revision":"8744778de2eb9add74224d57fac399aba6039a26","source_sha256":"5890af2c22e45994354f90ddf970ca7e4668dbdecb7553c48f0b7822d89a2cc7","sha256":"$hypr_sha","signature":"unsigned"},
    {"name":"$(basename -- "$ttfx_archive")","package":"ttfx","version":"0.3.2-1","architecture":"aarch64","source_revision":"7203e35","source_sha256":"d0c0df4867e7f03142fb7f77c66670d0e8da15534239c1a7abfd89f19dfc00f6","sha256":"$ttfx_sha","signature":"unsigned"}
  ]
}
EOF

"$builder" --check --output "$fixture" --metadata "$metadata" >/dev/null ||
  fail "bundle checker validates archive and manifest checksums"
pass "custom package bundle interface validates offline archives"
