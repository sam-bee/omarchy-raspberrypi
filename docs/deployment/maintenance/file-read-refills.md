# Bounded cold-refill comparison

`compare-file-refills.py` is the reviewed runner for the bounded offline-root
experiment. It runs one buffered/direct/buffered baseline pass and exactly three
file-specific refill cycles over the same version-1 signed manifest. The
runner is diagnostic evidence, not a repair tool, benchmark, or claim that a
clean run disproves the historical fault.

## Production boundary

Run it from a separate test OS as root in the initial user namespace. `--root`
must name the offline filesystem mounted read-only and may never be `/`. The
runner refuses non-root callers, a non-initial user namespace, writable target
mounts, output inside the target, non-regular files, symlink components,
changed device/inode/metadata, and target files that are neither root-owned
nor accessible through `CAP_FOWNER` in that same namespace. It has no fixture
or writable-target command-line bypass.

The output directory must be new and on a disk-backed filesystem. The runner
creates private mode-0700 output, fsynced `context.json`, `manifest.json`, and
JSONL evidence. It also creates a small private disk-backed residency control
file in that directory. The control is read, measured with the same
read-only, non-executable `mincore` mapping, advised with
`POSIX_FADV_DONTNEED` (offset zero and length zero, meaning through EOF), and
measured again. Unless it observes populated pages
followed by zero resident pages, the run stops as unsupported before touching
the offline files. The control is never put on tmpfs and no live file is used
for it.

Example outer supervision (the five-second kill grace is owned by `timeout`):

```sh
timeout --signal=TERM --kill-after=5s 180s \
  python3 compare-file-refills.py \
    --root /run/omarchy-read-comparison/nvme-root \
    --manifest /var/tmp/omarchy-diagnosis-20260925/verified-files.json \
    --output /var/tmp/read-refills-20260925
```

Keep the output on the test OS. Do not launch target executables, mount the
target read-write, call `drop_caches`, evict a whole filesystem, or change
boot, kernel, firmware, encryption, credentials, or PCIe state. The runner
only calls file-specific `POSIX_FADV_DONTNEED` on an open read-only target
descriptor after proving that its device/inode is not mapped by a visible
process.

## Durable baseline checkpoint

After the complete baseline passes, the runner fsyncs the results file and
output directory, hashes the durable JSONL prefix, and prints a checkpoint
record like this:

```json
{"type":"baseline_checkpoint","status":"pass","manifest_sha256":"…","ack_token":"…"}
```

The supervising wrapper must copy or otherwise verify that prefix off the
machine, then write exactly `ACK <ack_token>` followed by a newline to the
runner's stdin. The runner accepts no other input and performs no command
interpretation. It will not issue an eviction request until that acknowledgement
has arrived. EOF, a malformed line, or the remaining 180-second deadline
stops the run with no refill. This checkpoint closes the evidence-preservation
gap between the baseline and the first advisory cache operation.

TERM and INT handling is best effort. A signal raises a structured error at
the next safe Python boundary and partial streams are retained where the
comparison helper has begun a read. The outer supervisor may still kill the
process before a final summary; such a run is incomplete and must be
classified from the surviving output and exit status.

## Refill phases and evidence

The manifest order is pinned. Every entry is checked against its initial
device, inode, mode, owner, size, link count, and timestamps before every
phase. `/proc/<pid>/maps` is parsed by device and inode for every visible
process; unreadable or malformed maps fail closed, while a process that
vanishes during the scan is ignored. The temporary residency mapping is
always unmapped before this scan and before `POSIX_FADV_DONTNEED`.

For each cycle and file the runner records:

1. read-only `mincore` residency before the advisory request;
2. the file-specific fadvise range and result;
3. residency immediately afterward, requiring zero resident file pages before
   describing the following comparison as a cold refill;
4. the inherited buffered/direct/buffered comparison and signed digest checks;
5. final residency and fstat values.

If an EOF page remains resident after fadvise, the result is
`cold_range_unsupported`; the runner records that a partial EOF tail or
advisory retention may explain it and does not make a cold-read claim. No
global cache operation is attempted.

The aggregate hard limit is 180 seconds and 384 MiB of target logical
buffered bytes plus rounded direct-I/O request bytes over the baseline and all
three cycles. The small control read is included in the preflight budget. The
summary's `completed_*_bytes` fields count only completed comparison files and
exclude partial stream bytes retained after interruption. A baseline mismatch
or error stops before any refill. A first
complete comparison mismatch retains all three streams. A short read,
metadata error, timeout, or signal after a comparison has begun retains
available `.partial.bin` streams and the exact phase/offset in JSONL. An
unsupported pre-read phase has no comparison stream to retain; its durable
JSONL preparation record is the evidence. The runner stops at that first
failure and does not collect more examples.

## Local validation

The production CLI must not be run against a writable fixture or with a fake
root flag. A local integration harness may create a root-owned read-only bind
mount on a disk-backed ext4 fixture inside an isolated namespace and patch
only `_initial_user_namespace()` to represent that test namespace. It must
leave `check_root_context()`'s root and capability checks, the read-only
mount, ownership checks, mapping scan, mincore implementation, fadvise call,
and output-filesystem check active. The isolated namespace is explicitly not
evidence for an initial-namespace production run.

The existing `compare-file-reads.py` fixture tests remain the unit coverage for
aligned direct I/O, capture retention, path safety, and ordinary helper
refusal paths. The refill runner should at minimum be syntax-checked and
tested for its non-root/initial-namespace refusal before any Pi execution.
