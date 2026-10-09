# Omarchy Pi 5 0.2.0-pi5-2026.10.09

This production release packages the Raspberry Pi 5 installer image with the
package-owned Omarchy runtime and the updater and access fixes validated on the
accepted NVMe installation.

## What changed

- The desktop uses the conventional package layout: `/usr/share/omarchy` is
  owned by the `omarchy`/`omarchy-settings` package pair, with user-facing
  commands exposed through `/usr/bin`.
- The packaged updater now checks the pinned source resolver and installed
  source marker without creating a polling candidate, and the update worker
  snapshots the previous package pair and makes old-release migrations
  readable before the transaction.
- The installer preserves the SSH ownership correction: the account's
  `.ssh` directory and public-key files have the expected user ownership and
  restrictive modes while key-only access remains available.
- The image carries 337 installer package records and the Omarchy pair
  `4.0.0.alpha-6865` with source revision
  `c64c78415e97fd8b578ef0a9bcde9ad94a8c195e`.

## Image and provenance

- Raw image: `omarchy-pi5-0.2.0-pi5-2026.10.09-installer.img`; 14,643,363,840 bytes; SHA-256 `6e7dd757a564a6e688bc50be5b3b6ddb04f6c9a2de23cbeba5ada81b48ddab01`
- Reconstructed zstd stream: `omarchy-pi5-0.2.0-pi5-2026.10.09-installer.img.zst`; 7,115,222,871 bytes; SHA-256 `7404d88cd74e811d41dafb138a0a98f7a78ba7279a7d7d91dc7517ad5e20234a`
- Installer runtime source: `c64c78415e97fd8b578ef0a9bcde9ad94a8c195e`
- Installer runtime digest: `09d253faa20eaa20757ac5045641613b5b742b4bfe5858b6dde9ffee71d6724f`
- Desktop payload source: `c64c78415e97fd8b578ef0a9bcde9ad94a8c195e`
- Desktop payload: 635 installed package records; 2,974,104,624 bytes; SHA-256 `c862d85707a03e10c9ca20ef591461cf8fe171790bc2c7c34ef1d25f67c627bf`
- Acceptance record: `passed`; exact-image native boot tested: `false`; scope: Fresh encrypted NVMe installation from the accepted USB installer baseline, Matching final Omarchy runtime packages installed and normal Omarchy update completed, Updated NVMe reboot, automatic existing USB-key unlock, SSH, desktop startup and RDP service verified, Final image filesystem, file readback, runtime package provenance and unchanged boot prefix verified

The compressed stream is split below GitHub's 2 GiB asset limit:

- `omarchy-pi5-0.2.0-pi5-2026.10.09-installer.img.zst.part-00` — 1,900,000,000 bytes; SHA-256 `b52f4c0083ce7a1d14c2a5f0f89a010532296ef992f5f859dde8c775ac60d4ea`
- `omarchy-pi5-0.2.0-pi5-2026.10.09-installer.img.zst.part-01` — 1,900,000,000 bytes; SHA-256 `499319b49f300f548332ed9b71cab1a4616bb02ef694db0085c40734e1e9e019`
- `omarchy-pi5-0.2.0-pi5-2026.10.09-installer.img.zst.part-02` — 1,900,000,000 bytes; SHA-256 `0d2f87fc64ea63c9bf956d3a6133353ab71b9e09dfa37e3ab38bb7a3bfa48940`
- `omarchy-pi5-0.2.0-pi5-2026.10.09-installer.img.zst.part-03` — 1,415,222,871 bytes; SHA-256 `146f9fb4a6552969164ebaafa47c920eb30347d898d492657753a4ce2779ee3f`

The raw image and unsplit stream are recorded for provenance; publish the numbered parts and metadata files as release assets.

## Reconstruct and verify the image

Download every numbered part and `SHA256SUMS` into one directory, then run:

```sh
sha256sum --ignore-missing --check SHA256SUMS
cat omarchy-pi5-0.2.0-pi5-2026.10.09-installer.img.zst.part-00 \
    omarchy-pi5-0.2.0-pi5-2026.10.09-installer.img.zst.part-01 \
    omarchy-pi5-0.2.0-pi5-2026.10.09-installer.img.zst.part-02 \
    omarchy-pi5-0.2.0-pi5-2026.10.09-installer.img.zst.part-03 > omarchy-pi5-0.2.0-pi5-2026.10.09-installer.img.zst
sha256sum omarchy-pi5-0.2.0-pi5-2026.10.09-installer.img.zst
zstd --test omarchy-pi5-0.2.0-pi5-2026.10.09-installer.img.zst
zstd --decompress --keep omarchy-pi5-0.2.0-pi5-2026.10.09-installer.img.zst
sha256sum omarchy-pi5-0.2.0-pi5-2026.10.09-installer.img
```

The stream and raw hashes must match the values above. Write the resulting `.img` to the whole approved installer USB only after the separate hardware and media checks are complete.

## Packaged Omarchy runtime

The desktop uses the package-owned `/usr/share/omarchy` runtime and records the exact Omarchy package pair in `package-versions.json`:

- `omarchy` `4.0.0.alpha-6865` (aarch64); archive SHA-256 `e07f48cf58fcfabb8e7501ac38cbc26b69f5d2774f82735c8c65ed357ac4cb31`
- `omarchy-settings` `4.0.0.alpha-6865` (aarch64); archive SHA-256 `bac6505430c5f8f266a612906b23f9355f79028d0bf4cec7e696d6927a42482f`

The package pair and Pi-specific installer source are part of the image provenance. Use the packaged update path documented by the matching source revision; do not substitute a user-owned source checkout when comparing this release.

## Build and acceptance

The release manifest and package inventories record the image, package,
desktop, source and acceptance pins. The exact rebuilt USB image was verified
as files but was not booted natively; acceptance inherits the earlier full
encrypted installation and verifies the final runtime update, reboot, SSH,
desktop/RDP startup, filesystem, package provenance and unchanged boot prefix.

## License

See `LICENSES.md`. Omarchy source and project-specific changes are released under MIT. Bundled Arch Linux ARM packages, Raspberry Pi firmware and third-party components retain their respective licenses; their license texts remain in `/usr/share/licenses/<package>/` in the installed image.
