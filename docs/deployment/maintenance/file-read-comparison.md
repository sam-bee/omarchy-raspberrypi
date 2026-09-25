# Bounded buffered/direct file comparison

`compare-file-reads.py` compares normal buffered reads and aligned `O_DIRECT` reads against an independently supplied SHA-256 manifest. It is intended for an offline filesystem mounted read-only from a separate test OS. It does not mount filesystems, unlock disks, change kernels, evict caches, run target executables or repair files.

This is a diagnostic sample, not a disk benchmark or stability certification. A previously bad buffered read can disappear after reboot because that reboot discards the old cache. Passing a new run does not explain or repair the earlier discrepancy.

## Prepare the reference

Derive each expected size and digest from the exact installed package version after verifying its archive signature against an established trusted keyring. Preserve the archive, signature, verifier output and manifest off the target. A hash from the target's installed package metadata alone is not an independent reference.

The manifest contains regular-file paths relative to the supplied root:

```json
{
  "version": 1,
  "files": [
    {
      "path": "usr/lib/example.so",
      "size": 5000,
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  ]
}
```

The example digest is a placeholder. Use actual verified package-member values. Choose a small fixed corpus, including the previously affected file and neighboring controls, and keep it identical across runs. Do not execute any of these files.

## Run a control

First verify the running test OS, target device identity, read-only mapping and read-only filesystem mount. An encrypted ext4 target should be opened read-only and mounted with journal replay disabled. Check that it was cleanly unmounted before interpreting filesystem contents; an unclean filesystem requires a separate recovery decision. Keep the target's boot partition unmounted. Collect boot identity, exact kernel version/build ID, mount state, relevant kernel errors and temperature before the probe.

Use a new private output directory on the test OS, outside the target filesystem:

```bash
timeout --signal=TERM --kill-after=5s 310s python3 compare-file-reads.py \
  --root /run/read-comparison/nvme-root \
  --manifest verified-files.json \
  --output /var/tmp/read-comparison-run-1 \
  --rounds 3
```

Run as the ordinary test user when the selected package files are readable. Inspect the command's status and the output records; incomplete or unsupported direct-I/O measurements are errors, not agreement. The probe's writable-fixture mode exists only for local testing and must not be used for the offline NVMe comparison.

Each observation performs a buffered read, an aligned direct read and a second buffered read of the same inode, checking metadata stability and exact logical byte counts. The first observed read after a fresh boot is distinct from subsequent warm reads, but is not asserted to be a guaranteed cold device read. The probe does not clear either filesystem or device caches.

The probe accepts at most five rounds, a 300-second wall-clock bound, 2 GiB of planned read requests and 64 MiB per manifest file. To guarantee that all three complete streams fit in the 128 MiB raw evidence bound, its mismatch-capture limit is 32 MiB per file. `--max-total-bytes` and `--max-seconds` may lower those limits for a fixture test, never raise them.

On the first mismatch or error, stop the experiment and preserve its output off-device. Mismatching streams are retained in full; review those bytes alongside the trusted manifest and metadata. Do not respond by restarting, dropping caches or replacing the suspect file. Record access, service and kernel-log state afterward without launching an additional workload.

## Interpret the comparison

| Observation | Supported conclusion |
| --- | --- |
| Buffered reads differ from the signed reference while the direct read matches | A buffered/direct discrepancy was observed in this sample; the underlying cause is still open. |
| Direct read differs from the signed reference | The issue is not limited to the observed buffered bytes; preserve the stream and investigate further. |
| Both paths match the reference | No discrepancy was observed for these files during this run. |
| File metadata, sizes or read completion are inconsistent | The measurement is inconclusive; do not classify it as a clean read or corruption. |

Begin with the same kernel on the separate test root. This controls for running from NVMe and provides the baseline for any later kernel experiment. A comparison with another kernel is useful only after confirming that the original condition or a defined trigger reproduces. Otherwise both kernels may simply pass a weak sample. Keep firmware, kernel parameters, hardware and corpus fixed; change one variable at a time and document unavoidable differences.

The helper has focused local fixtures under `test/shell.d/arm64-file-read-comparison-test.sh`. Local fixture results establish measurement behavior and refusal paths; they do not constitute Raspberry Pi or NVMe acceptance.
