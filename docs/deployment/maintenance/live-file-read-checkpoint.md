# Live-root file-read checkpoint

`compare-live-file-reads.py` takes one bounded read-only checkpoint from the running Pi root after the parent launch guards have established the expected encrypted NVMe device and desktop state. It is for the startup investigation and is a diagnostic observation, not a repair, benchmark, or stability certification.

The wrapper fixes the target root to `/` and refuses to mount anything. It opens only regular files named by the supplied version-1 manifest, uses `O_NOFOLLOW` for every path component, and performs one buffered read, one aligned `O_DIRECT` read, and a second buffered read for each file. It never writes the compared files, executes target binaries, calls `posix_fadvise`, drops or flushes caches, or changes mounts. The first and second buffered reads can warm the live page cache; that is an expected measurement effect and means this is not a cold-cache test. A live writer can change a file between the two reads, so the wrapper pins and compares `fstat` identity before and after the complete stream and stops on the first metadata or byte mismatch.

The wrapper checks the initial user namespace, exact expected boot ID, exact expected kernel release, an ext4 root mounted from `/dev/mapper/cryptroot`, and the supplied root filesystem UUID. The parent launch guards must establish that `cryptroot` is backed by the expected NVMe LUKS UUID and hold the required boot/readiness state. The `--phase` value (`pre-desktop` or `post-desktop`) is recorded metadata only; both checkpoints use the same measurement.

The manifest is prepared outside this wrapper from package members whose archives and signatures were verified during review. The reviewed three-file corpus is NATS-DANO, Hyprland, and Qt; the parent launch prepares the local `prepared-three-files.json` manifest. The wrapper accepts at most three entries and rejects non-regular files, size or digest errors, duplicate paths, absolute paths, `.`/`..` components, and symlink components. Its one-pass planned request budget is 64 MiB and its internal wall-clock bound is 30 seconds. Run it with an outer five-second termination grace period:

```bash
timeout --signal=TERM --kill-after=5s 30s python3 compare-live-file-reads.py \
  --manifest prepared-three-files.json \
  --expected-boot-id "$EXPECTED_BOOT_ID" \
  --expected-root-uuid "$EXPECTED_ROOT_UUID" \
  --expected-kernel-release "$EXPECTED_KERNEL_RELEASE" \
  --phase post-desktop
```

Each invocation creates a private new directory below `/var/tmp/omarchy-nvme-startup-check/`, preserves the exact manifest, and writes `results.jsonl` with fsync. These evidence writes occur on the live filesystem under investigation and are the wrapper's intended local write activity; the compared files remain untouched. A passing file has all three digests equal to the manifest digest and stable metadata. The first mismatch or metadata change stops the run. A completed three-stream capture is retained for a mismatch; an interrupted or failed stream is retained as `.partial` through the neighboring comparison helper. The summary records the boot ID, uptime, kernel release, root and mount context, phase, and actual command. TERM/INT handling is best effort; the outer timeout's five-second kill grace is the hard containment boundary. Preserve the output off-device before any reboot or further workload.

Interpret a pass narrowly: no discrepancy was observed for these files during this live run. A mismatch is evidence about this sample and its concurrent writer/cache state; it does not identify the underlying cause. Do not respond by restarting, dropping caches, replacing a file, or launching a target workload. The checkpoint itself warms buffered pages and cannot provide a cold-device claim.
