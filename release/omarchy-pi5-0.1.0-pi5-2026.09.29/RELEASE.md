# Omarchy Pi 5 0.1.0-pi5-2026.09.29

This release packages the installer image exercised in the complete Raspberry Pi 5 installation rehearsal on 29 September 2026. The release contains the same compressed byte stream that was verified before the candidate-02 USB flash; it is split into three assets because GitHub release assets have a 2 GiB limit.

## Image and provenance

- Raw image: 14,643,363,840 bytes; SHA-256 `4d7c23ebc32ddc6499db4ebe526a1e645bf44abf1212ee16dd4206fbd6543714`
- Reconstructed zstd stream: 4,628,036,661 bytes; SHA-256 `6ba0293062e2f30c1d4df70a6e6fb0992a81a6bd27202f8337e892d751ccbcab`
- Installer runtime source: `e62e596de97fe91fae7b61a3af797450eaa61f27`
- Installer runtime digest: `b9b25a28bbeed06f66100a604a744b5984095f523b62489cee5ed1ee3c52a2fc`
- Desktop payload source: `fcb2b5afcb8c06b6daf93319792f072f9111d669`
- Desktop payload: 633 installed package records, 2,800,963,177 bytes, SHA-256 `1f640ef4641cf0a2086956d9b7c51368b5d95a9a853b31bf4dfcd27c1637042c`

The installer implementation and build guide are in the repository's [`install/arm64/installer-image/README.md`](../../install/arm64/installer-image/README.md).

## Reconstruct the image

Download all three `.part-*` assets and `SHA256SUMS` into one directory. Check the parts, then concatenate them in numeric order:

```sh
sha256sum --ignore-missing --check SHA256SUMS
cat omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img.zst.part-00 \
    omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img.zst.part-01 \
    omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img.zst.part-02 \
    > omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img.zst
sha256sum omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img.zst
zstd --decompress --keep omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img.zst
sha256sum omarchy-pi5-0.1.0-pi5-2026.09.29-installer.img
```

The two final hashes must match the values above. The resulting `.img` is the file to write to the whole installer USB.

## Build reference

The [image builder](https://github.com/sam-bee/omarchy-raspberrypi/blob/e62e596de97fe91fae7b61a3af797450eaa61f27/install/arm64/installer-image/build-installer-image.py) is available at the tested installer runtime revision. The builder records the signed rootfs, package transaction, desktop payload, runtime digest and image verification in its build manifest. Use a fresh workdir and output path for each native aarch64 build, review the plan, then run the same command with `--apply` as described in the [builder guide](https://github.com/sam-bee/omarchy-raspberrypi/blob/e62e596de97fe91fae7b61a3af797450eaa61f27/install/arm64/installer-image/README.md).
