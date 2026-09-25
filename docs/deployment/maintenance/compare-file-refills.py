#!/usr/bin/env python3
"""Run a bounded, file-specific page-cache refill comparison.

This is the production runner for the offline-root experiment described in
``file-read-refills.md``.  It deliberately has no writable-fixture command
line mode: the target must be a separate read-only filesystem and the caller
must be root in the initial user namespace.  The ordinary comparison helper
is loaded from the neighbouring ``compare-file-reads.py`` file so that path,
direct-I/O, metadata and capture checks remain in one place.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import glob
import hashlib
import importlib.util
import json
import mmap as py_mmap
import os
import re
import signal
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
HELPER_PATH = HERE / "compare-file-reads.py"
SPEC = importlib.util.spec_from_file_location("compare_file_reads", HELPER_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - installation failure
    raise RuntimeError(f"cannot load comparison helper: {HELPER_PATH}")
HELPER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HELPER
SPEC.loader.exec_module(HELPER)


MAX_SECONDS = 180.0
MAX_TOTAL_BYTES = 384 * 1024 * 1024
REFILL_CYCLES = 3
BASELINE_ROUND = 0
TMPFS_MAGIC = 0x01021994
CAP_FOWNER = 3
MAPS_LINE = re.compile(r"^[0-9a-f]+-[0-9a-f]+\s+\S+\s+\S+\s+(?P<dev>[0-9a-fA-F]+:[0-9a-fA-F]+)\s+(?P<ino>\d+)(?:\s|$)")


class RunnerError(HELPER.ProbeError):
    """An error raised by a refill phase or its production guards."""


@dataclass(frozen=True)
class PinnedEntry:
    entry: Any
    identity: tuple[int, ...]
    stat: dict[str, int]
    planned_bytes: int


_stop_signal: int | None = None


def _signal_stop(signum: int, _frame: Any) -> None:
    """Best-effort TERM handling; the outer supervisor still owns the bound."""

    global _stop_signal
    _stop_signal = signum
    raise RunnerError("signal", f"received signal {signum}; stopping at the next safe point", signal=signum)


def check_deadline(deadline: float) -> None:
    if _stop_signal is not None:
        raise RunnerError("signal", f"received signal {_stop_signal}; stopping", signal=_stop_signal)
    if time.monotonic() >= deadline:
        raise RunnerError("time_limit", "probe runtime limit reached")


def _initial_user_namespace() -> bool:
    """Return true only for the normal initial user namespace."""

    try:
        with open("/proc/self/uid_map", encoding="ascii") as stream:
            rows = [line.split() for line in stream if line.split()]
        if rows != [["0", "0", "4294967295"]]:
            return False
        return os.stat("/proc/self/ns/user").st_ino == os.stat("/proc/1/ns/user").st_ino
    except (OSError, ValueError):
        return False


def check_root_context() -> dict[str, Any]:
    """Require root ownership authority in the initial user namespace.

    This function is intentionally isolated so a local integration harness can
    mock only this production boundary inside an explicitly isolated namespace.
    The command-line runner never exposes a bypass for it.
    """

    if os.geteuid() != 0:
        raise RunnerError("root_required", "the refill runner must run as root")
    if not _initial_user_namespace():
        raise RunnerError("initial_user_namespace_required", "the refill runner requires the initial user namespace")
    cap_eff = None
    try:
        with open("/proc/self/status", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("CapEff:"):
                    cap_eff = int(line.split()[1], 16)
                    break
    except (OSError, ValueError):
        pass
    return {
        "euid": os.geteuid(),
        "egid": os.getegid(),
        "initial_user_namespace": True,
        "cap_fowner": bool(cap_eff is not None and (cap_eff & (1 << CAP_FOWNER))),
    }


def file_authority(st: os.stat_result, context: dict[str, Any], path: str) -> dict[str, bool]:
    """Verify root owns the target or has CAP_FOWNER in the same namespace."""

    owned = int(st.st_uid) == int(context["euid"])
    if not owned and not context["cap_fowner"]:
        raise RunnerError(
            "file_authority",
            f"caller neither owns root file nor has CAP_FOWNER for {path}",
            path=path,
            file_uid=int(st.st_uid),
            euid=int(context["euid"]),
        )
    return {"caller_owns_file": owned, "cap_fowner": bool(context["cap_fowner"])}


def _libc() -> Any:
    return ctypes.CDLL(None, use_errno=True)


def statfs_type(path: str) -> int:
    class StatFs(ctypes.Structure):
        _fields_ = [
            ("f_type", ctypes.c_long),
            ("f_bsize", ctypes.c_long),
            ("f_blocks", ctypes.c_ulong),
            ("f_bfree", ctypes.c_ulong),
            ("f_bavail", ctypes.c_ulong),
            ("f_files", ctypes.c_ulong),
            ("f_ffree", ctypes.c_ulong),
            ("f_fsid", ctypes.c_int * 2),
            ("f_namelen", ctypes.c_long),
            ("f_frsize", ctypes.c_long),
            ("f_flags", ctypes.c_long),
            ("f_spare", ctypes.c_long * 4),
        ]

    libc = _libc()
    call = libc.statfs
    call.argtypes = [ctypes.c_char_p, ctypes.POINTER(StatFs)]
    call.restype = ctypes.c_int
    result = StatFs()
    if call(os.fsencode(path), ctypes.byref(result)) != 0:
        error = ctypes.get_errno()
        raise RunnerError("output_filesystem", f"cannot inspect output filesystem: [{error}] {os.strerror(error)}", errno=error)
    return int(result.f_type) & ((1 << (ctypes.sizeof(ctypes.c_long) * 8)) - 1)


def require_disk_output_parent(path: str) -> dict[str, Any]:
    parent = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(parent):
        raise RunnerError("output_error", f"output parent is not a directory: {parent}")
    f_type = statfs_type(parent)
    if f_type == TMPFS_MAGIC:
        raise RunnerError("output_tmpfs", "output and residency control must be on a disk-backed filesystem, not tmpfs")
    return {"parent": parent, "filesystem_type": hex(f_type)}


def _mincore_call(fd: int, size: int) -> dict[str, Any]:
    """Query file-page residency through a read-only, non-executable mapping."""

    page = int(py_mmap.PAGESIZE)
    pages = (size + page - 1) // page if size else 0
    if pages == 0:
        return {"pages": 0, "resident_pages": 0, "map_length": 0, "mapping": "read-only-nonexec"}
    length = pages * page
    libc = _libc()
    mmap_call = libc.mmap
    mmap_call.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_longlong]
    mmap_call.restype = ctypes.c_void_p
    munmap_call = libc.munmap
    munmap_call.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    munmap_call.restype = ctypes.c_int
    mincore_call = libc.mincore
    mincore_call.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
    mincore_call.restype = ctypes.c_int

    address = mmap_call(None, length, int(py_mmap.PROT_READ), int(py_mmap.MAP_PRIVATE), fd, 0)
    failed = ctypes.c_void_p(-1).value
    if address is None or ctypes.cast(address, ctypes.c_void_p).value == failed:
        error = ctypes.get_errno()
        raise RunnerError("mincore_map", f"read-only residency mapping failed: [{error}] {os.strerror(error)}", errno=error)
    try:
        vector = (ctypes.c_ubyte * pages)()
        ctypes.set_errno(0)
        if mincore_call(address, length, vector) != 0:
            error = ctypes.get_errno()
            raise RunnerError("mincore_error", f"mincore failed: [{error}] {os.strerror(error)}", errno=error)
        resident = sum(int(item) & 1 for item in vector)
        return {"pages": pages, "resident_pages": resident, "map_length": length, "mapping": "read-only-nonexec"}
    finally:
        if munmap_call(address, length) != 0:
            error = ctypes.get_errno()
            raise RunnerError("mincore_unmap", f"residency mapping unmap failed: [{error}] {os.strerror(error)}", errno=error)


def residency(fd: int, size: int) -> dict[str, Any]:
    try:
        return _mincore_call(fd, size)
    except RunnerError:
        raise
    except OSError as exc:
        raise RunnerError("mincore_error", str(exc), errno=exc.errno) from exc


def fadvise_dontneed(fd: int, size: int) -> dict[str, Any]:
    function = getattr(os, "posix_fadvise", None)
    advice = getattr(os, "POSIX_FADV_DONTNEED", None)
    if function is None or advice is None:
        raise RunnerError("fadvise_unavailable", "Python does not expose POSIX_FADV_DONTNEED")
    try:
        # POSIX defines length zero as "to end of file".  It also makes the
        # intent unambiguous for a final partial page; the residency result,
        # rather than an assumed byte range, decides whether the refill is
        # described as cold.
        result = function(fd, 0, 0, advice)
    except OSError as exc:
        raise RunnerError("fadvise_error", f"POSIX_FADV_DONTNEED failed: {exc}", errno=exc.errno, requested_bytes=0, target_size=size) from exc
    if result not in (None, 0):
        raise RunnerError("fadvise_error", f"POSIX_FADV_DONTNEED returned {result}", result=int(result), requested_bytes=0, target_size=size)
    return {"advice": "POSIX_FADV_DONTNEED", "offset": 0, "length": 0, "target_size": size, "result": 0}


def _device_string(dev: int) -> str:
    return f"{os.major(dev):x}:{os.minor(dev):x}"


def scan_proc_maps(dev: int, ino: int) -> dict[str, Any]:
    """Fail closed unless every visible process map can be read and parsed."""

    wanted_numbers = (os.major(dev), os.minor(dev), int(ino))
    scanned = 0
    matches: list[dict[str, Any]] = []
    try:
        process_entries = list(os.scandir("/proc"))
    except OSError as exc:
        raise RunnerError("maps_visibility", f"cannot enumerate /proc: {exc}", errno=exc.errno) from exc
    for process in process_entries:
        if not process.name.isdigit():
            continue
        maps_path = os.path.join("/proc", process.name, "maps")
        try:
            fd = os.open(maps_path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ESRCH):
                continue
            raise RunnerError("maps_visibility", f"cannot read {maps_path}: {exc}", errno=exc.errno, pid=int(process.name)) from exc
        scanned += 1
        try:
            with os.fdopen(fd, "r", encoding="ascii", errors="strict") as stream:
                for line_number, line in enumerate(stream, 1):
                    match = MAPS_LINE.match(line)
                    if match is None:
                        raise RunnerError("maps_parse", f"cannot parse {maps_path}:{line_number}", pid=int(process.name), line=line.rstrip())
                    major, minor = (int(part, 16) for part in match.group("dev").split(":", 1))
                    if (major, minor, int(match.group("ino"))) == wanted_numbers:
                        matches.append({"pid": int(process.name), "line": line.rstrip()})
        except UnicodeError as exc:
            raise RunnerError("maps_parse", f"non-ASCII data in {maps_path}: {exc}", pid=int(process.name)) from exc
    return {"device": _device_string(dev), "inode": ino, "processes_scanned": scanned, "matches": matches, "mapped": bool(matches)}


def open_pinned(root_fd: int, pinned: PinnedEntry) -> tuple[int, os.stat_result, dict[str, bool]]:
    fd = HELPER.open_relative(root_fd, pinned.entry.path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        HELPER.check_fd_mount(fd, pinned.entry.path, False)
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode):
            raise RunnerError("not_regular_file", f"manifest path is no longer a regular file: {pinned.entry.path}")
        if HELPER.stable(current) != pinned.identity:
            raise RunnerError("pinned_identity_changed", f"pinned file identity changed for {pinned.entry.path}", path=pinned.entry.path)
        authority = file_authority(current, ROOT_CONTEXT, pinned.entry.path)
        return fd, current, authority
    except BaseException:
        os.close(fd)
        raise


def fsync_directory(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_logger(logger: Any, output: str) -> None:
    os.fsync(logger.fd)
    fsync_directory(output)


def write_private(path: str, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(content)
        while view:
            count = os.write(fd, view)
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def run_residency_control(output: str, deadline: float) -> dict[str, Any]:
    """Validate mincore/fadvise against a private disk-backed fixture."""

    check_deadline(deadline)
    path = os.path.join(output, "residency-control.bin")
    size = 4 * int(py_mmap.PAGESIZE)
    pattern = bytes((index * 29 + 7) % 251 for index in range(size))
    write_private(path, pattern)
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RunnerError("residency_control", "control fixture is not regular")
        if len(os.pread(fd, size, 0)) != size:
            raise RunnerError("residency_control", "disk fixture read was short")
        populated = residency(fd, size)
        if populated["resident_pages"] <= 0:
            raise RunnerError("residency_control", "mincore did not observe the populated disk fixture", before=populated)
        advice = fadvise_dontneed(fd, size)
        after = residency(fd, size)
        if after["resident_pages"] != 0:
            raise RunnerError(
                "residency_control",
                "disk fixture did not demonstrate a zero-resident fadvise transition",
                before=populated,
                after=after,
            )
        record = {"path": path, "size": size, "before": populated, "fadvise": advice, "after": after, "disk_backed": True}
        write_private(os.path.join(output, "residency-control.json"), json.dumps(record, sort_keys=True, indent=2).encode() + b"\n")
        return record
    finally:
        os.close(fd)


def partial_captures(output: str, index: int, round_number: int) -> list[str]:
    prefix = os.path.join(output, f"file-{index:03d}-round-{round_number}-")
    return sorted(glob.glob(prefix + "*.partial.bin"))


def stat_record_identity(value: dict[str, int]) -> tuple[int, ...]:
    return tuple(int(value[name]) for name in ("st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns"))


def pin_compare_record(record: dict[str, Any], item: PinnedEntry) -> bool:
    fstat = record.get("fstat")
    if not isinstance(fstat, dict):
        record["pinned_identity_match"] = False
        return False
    values = [fstat.get(name) for name in ("before_buffered", "before_direct", "after_buffered", "after_direct")]
    valid = all(isinstance(value, dict) and stat_record_identity(value) == item.identity for value in values)
    record["pinned_identity_match"] = valid
    return valid


def await_baseline_ack(output: str, logger: Any, manifest_digest: str, deadline: float) -> str:
    """Require an operator/wrapper acknowledgement before the first fadvise.

    The token is the hash of the durable results prefix through
    ``baseline_complete``.  Only the exact ``ACK <token>`` line is accepted;
    stdin is never interpreted as a command.
    """

    check_deadline(deadline)
    with open(os.path.join(output, "results.jsonl"), "rb") as stream:
        token = hashlib.sha256(stream.read()).hexdigest()
    HELPER.emit(
        {
            "type": "baseline_checkpoint",
            "status": "pass",
            "manifest_sha256": manifest_digest,
            "ack_token": token,
            "message": "baseline evidence is durable; acknowledge before file-specific fadvise",
        },
        logger,
    )
    sync_logger(logger, output)
    import select

    remaining = max(0.0, deadline - time.monotonic())
    readable, _unused, _error = select.select([sys.stdin], [], [], remaining)
    if not readable:
        raise RunnerError("baseline_ack_timeout", "timed out waiting for the baseline checkpoint acknowledgement")
    response = sys.stdin.readline(256)
    if response != f"ACK {token}\n":
        raise RunnerError("baseline_ack_invalid", "baseline acknowledgement did not match the durable checkpoint token")
    HELPER.emit({"type": "baseline_checkpoint", "status": "acknowledged", "manifest_sha256": manifest_digest, "ack_token": token}, logger)
    sync_logger(logger, output)
    return token


def load_entries(root_fd: int, root: str, manifest_bytes: bytes, raw_entries: list[tuple[str, int, str]], deadline: float) -> tuple[list[PinnedEntry], int]:
    entries: list[PinnedEntry] = []
    one_cycle = 0
    for index, (relative, expected_size, digest) in enumerate(raw_entries, 1):
        check_deadline(deadline)
        candidate = os.path.join(root, relative)
        HELPER.check_mount(candidate, False)
        fd = HELPER.open_relative(root_fd, relative, os.O_RDONLY | os.O_NONBLOCK)
        try:
            HELPER.check_fd_mount(fd, candidate, False)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise RunnerError("not_regular_file", f"manifest path is not regular: {relative}")
            if st.st_size != expected_size:
                raise RunnerError("size_mismatch", f"manifest size {expected_size} differs from {st.st_size} for {relative}")
            authority = file_authority(st, ROOT_CONTEXT, relative)
            alignment = HELPER.rounded(max(HELPER.PAGE_BYTES, int(st.st_blksize or HELPER.PAGE_BYTES)), py_mmap.PAGESIZE)
            chunk = HELPER.rounded(HELPER.BLOCK_BYTES, alignment)
            direct_requests = sum(HELPER.rounded(min(chunk, expected_size - offset), alignment) for offset in range(0, expected_size, chunk))
            one_cycle += 2 * expected_size + direct_requests
            entry = HELPER.Entry(relative, expected_size, digest, alignment, chunk)
            entries.append(PinnedEntry(entry, HELPER.stable(st), HELPER.stat_record(st), 2 * expected_size + direct_requests))
            _ = authority
        finally:
            os.close(fd)
    return entries, one_cycle


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="offline filesystem root; must be read-only and never /")
    parser.add_argument("--manifest", required=True, help="version-1 signed-file manifest")
    parser.add_argument("--output", required=True, help="new private disk-backed output directory")
    parser.add_argument("--max-seconds", type=float, default=MAX_SECONDS)
    parser.add_argument("--max-total-bytes", type=int, default=MAX_TOTAL_BYTES)
    args = parser.parse_args(argv)
    if not 0 < args.max_seconds <= MAX_SECONDS:
        parser.error(f"--max-seconds must be greater than zero and no more than {int(MAX_SECONDS)}")
    if not 0 < args.max_total_bytes <= MAX_TOTAL_BYTES:
        parser.error("--max-total-bytes must be positive and no more than 384 MiB")
    return args


ROOT_CONTEXT: dict[str, Any] = {}


def main(argv: list[str] | None = None) -> int:
    global ROOT_CONTEXT
    started = time.monotonic()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    deadline = started + args.max_seconds
    output = os.path.abspath(args.output)
    root_fd = -1
    logger: Any = None
    manifest_digest: str | None = None
    status = "error"
    results = 0
    used_bytes = 0
    target_planned = 0
    control_read_bytes = 0
    return_code = 1
    try:
        signal.signal(signal.SIGTERM, _signal_stop)
        signal.signal(signal.SIGINT, _signal_stop)
        ROOT_CONTEXT = check_root_context()
        root = os.path.realpath(args.root)
        if root == "/":
            raise RunnerError("root_guard", "--root may not be /")
        if not os.path.isdir(root):
            raise RunnerError("root_error", f"--root is not a directory: {args.root}")
        HELPER.check_mount(root, False)
        output_real = os.path.realpath(output)
        if os.path.commonpath((root, output_real)) == root:
            raise RunnerError("output_inside_root", "--output must be outside --root")
        output_fs = require_disk_output_parent(output)
        if os.path.lexists(output):
            raise RunnerError("output_exists", "--output must name a new directory")
        with open(args.manifest, "rb") as stream:
            manifest_bytes = stream.read()
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        raw_entries = HELPER.load_manifest(manifest_bytes)
        os.mkdir(output, 0o700)
        os.chmod(output, 0o700)
        write_private(os.path.join(output, "manifest.json"), manifest_bytes)
        initial_context = {
            "runner": os.path.basename(__file__),
            "root": root,
            "manifest_sha256": manifest_digest,
            "limits": {"max_seconds": args.max_seconds, "max_total_bytes": args.max_total_bytes, "baseline_passes": 1, "refill_cycles": REFILL_CYCLES},
            "page_size": py_mmap.PAGESIZE,
            "root_context": ROOT_CONTEXT,
            "output_filesystem": output_fs,
            "command": [sys.executable, __file__, *sys.argv[1:]],
        }
        write_private(os.path.join(output, "context.json"), json.dumps(initial_context, sort_keys=True, indent=2).encode() + b"\n")
        fsync_directory(output)
        logger = HELPER.JsonLogger(output)
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        HELPER.check_fd_mount(root_fd, root, False)
        entries, one_cycle = load_entries(root_fd, root, manifest_bytes, raw_entries, deadline)
        target_planned = (1 + REFILL_CYCLES) * one_cycle
        control_planned = 4 * int(py_mmap.PAGESIZE)
        if target_planned + control_planned > args.max_total_bytes:
            raise RunnerError("read_budget_exceeded", f"planned target and control reads require {target_planned + control_planned} bytes", planned_bytes=target_planned + control_planned, target_planned_bytes=target_planned, control_planned_bytes=control_planned, max_total_bytes=args.max_total_bytes)
        if any(item.entry.size > HELPER.MAX_CAPTURE_FILE_BYTES or item.entry.size * 3 > HELPER.MAX_OUTPUT_BYTES for item in entries):
            raise RunnerError("capture_budget_exceeded", "a complete three-stream capture exceeds the evidence bound")
        control = run_residency_control(output, deadline)
        control_read_bytes = int(control["size"])
        emit = HELPER.emit
        emit({"type": "residency_control", "status": "pass", **control}, logger)
        sync_logger(logger, output)
        emit({"type": "preflight", "status": "pass", "files": len(entries), "one_cycle_bytes": one_cycle, "target_planned_bytes": target_planned}, logger)
        sync_logger(logger, output)

        # Scan every target before the baseline and pin its stable identity.
        for item in entries:
            check_deadline(deadline)
            scan = scan_proc_maps(item.identity[0], item.identity[1])
            if scan["mapped"]:
                raise RunnerError("mapped_target", f"target inode is mapped before baseline: {item.entry.path}", path=item.entry.path, maps=scan)

        # One complete baseline pass.  Its durable log is the gate before any
        # file-specific advisory operation is allowed.
        for index, item in enumerate(entries, 1):
            check_deadline(deadline)
            fd, st, _authority = open_pinned(root_fd, item)
            os.close(fd)
            try:
                record = HELPER.compare_entry(root_fd, item.entry, index, BASELINE_ROUND, output, deadline)
            except HELPER.ProbeError as exc:
                details = dict(exc.details)
                details.update({"phase": "baseline", "path": item.entry.path, "partial": partial_captures(output, index, BASELINE_ROUND)})
                raise RunnerError(exc.kind, exc.message, **details) from exc
            record["phase"] = "baseline"
            record["pinned_identity"] = list(item.identity)
            if not pin_compare_record(record, item):
                HELPER.emit(record, logger)
                results += 1
                raise RunnerError("pinned_identity_changed", f"baseline comparison changed metadata for {item.entry.path}", path=item.entry.path, record=record)
            HELPER.emit(record, logger)
            results += 1
            used_bytes += item.planned_bytes
            if record["status"] != "pass":
                raise RunnerError("baseline_mismatch", f"baseline did not pass for {item.entry.path}", path=item.entry.path, record=record)
        HELPER.emit({"type": "baseline_complete", "status": "pass", "results": results, "read_bytes": used_bytes}, logger)
        sync_logger(logger, output)
        await_baseline_ack(output, logger, manifest_digest, deadline)

        for cycle in range(1, REFILL_CYCLES + 1):
            for index, item in enumerate(entries, 1):
                check_deadline(deadline)
                fd, before_stat, authority = open_pinned(root_fd, item)
                try:
                    before_residency = residency(fd, item.entry.size)
                finally:
                    os.close(fd)
                maps = scan_proc_maps(item.identity[0], item.identity[1])
                if maps["mapped"]:
                    raise RunnerError("mapped_target", f"target inode is mapped before fadvise: {item.entry.path}", path=item.entry.path, maps=maps, cycle=cycle)
                fd, checked_stat, _ = open_pinned(root_fd, item)
                try:
                    if HELPER.stable(checked_stat) != HELPER.stable(before_stat):
                        raise RunnerError("pinned_identity_changed", f"file changed between residency and fadvise: {item.entry.path}")
                    advice = fadvise_dontneed(fd, item.entry.size)
                    after_fadvise = residency(fd, item.entry.size)
                finally:
                    os.close(fd)
                cold = after_fadvise["resident_pages"] == 0
                prepare = {
                    "type": "refill_prepare",
                    "phase": "refill",
                    "cycle": cycle,
                    "path": item.entry.path,
                    "pinned_identity": list(item.identity),
                    "fstat_before": HELPER.stat_record(before_stat),
                    "authority": authority,
                    "maps": maps,
                    "residency_before": before_residency,
                    "fadvise": advice,
                    "residency_after_fadvise": after_fadvise,
                    "cold_range": cold,
                    "cold_explanation": "all mapped file pages were nonresident after fadvise" if cold else "nonzero pages remain; no cold-refill claim is made (EOF-tail or advisory retention may explain this)",
                }
                HELPER.emit(prepare, logger)
                sync_logger(logger, output)
                if not cold:
                    raise RunnerError("cold_range_unsupported", f"fadvise did not leave zero resident pages for {item.entry.path}", path=item.entry.path, cycle=cycle, residency=after_fadvise)

                check_deadline(deadline)
                try:
                    record = HELPER.compare_entry(root_fd, item.entry, index, cycle, output, deadline)
                except HELPER.ProbeError as exc:
                    details = dict(exc.details)
                    details.update({"phase": "refill", "cycle": cycle, "path": item.entry.path, "partial": partial_captures(output, index, cycle)})
                    raise RunnerError(exc.kind, exc.message, **details) from exc
                record["phase"] = "refill"
                record["cycle"] = cycle
                record["pinned_identity"] = list(item.identity)
                identity_match = pin_compare_record(record, item)
                fd, after_stat, _ = open_pinned(root_fd, item)
                try:
                    after_read = residency(fd, item.entry.size)
                finally:
                    os.close(fd)
                record["residency_after_read"] = after_read
                record["fstat_after_read"] = HELPER.stat_record(after_stat)
                record["cold_range"] = cold
                HELPER.emit(record, logger)
                results += 1
                if not identity_match:
                    raise RunnerError("pinned_identity_changed", f"refill comparison changed metadata for {item.entry.path}", path=item.entry.path, cycle=cycle, record=record)
                used_bytes += item.planned_bytes
                if record["status"] != "pass":
                    raise RunnerError("comparison_mismatch", f"refill comparison did not pass for {item.entry.path}", path=item.entry.path, cycle=cycle, record=record)
            sync_logger(logger, output)
        status = "pass"
        return_code = 0
    except HELPER.ProbeError as exc:
        error = {"kind": exc.kind, "message": exc.message, **exc.details}
        if logger is not None:
            try:
                HELPER.emit({"type": "error", "status": "error", "error": error}, logger)
            except Exception:
                pass
        else:
            HELPER.emit({"type": "error", "status": "error", "error": error}, None)
        status = "error" if exc.kind not in {"comparison_mismatch", "baseline_mismatch"} else "mismatch"
        return_code = 1
    except OSError as exc:
        error = {"kind": "io_error", "message": str(exc), "errno": exc.errno}
        if logger is not None:
            try:
                HELPER.emit({"type": "error", "status": "error", "error": error}, logger)
            except Exception:
                pass
        else:
            HELPER.emit({"type": "error", "status": "error", "error": error}, None)
        return_code = 1
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        summary = {
            "type": "summary",
            "status": status,
            "results": results,
            "baseline_passes": 1,
            "refill_cycles": REFILL_CYCLES,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "max_seconds": args.max_seconds,
            "max_total_bytes": args.max_total_bytes,
            "planned_target_bytes": target_planned,
            "control_read_bytes": control_read_bytes,
            "completed_target_bytes": used_bytes,
            "completed_read_bytes": used_bytes + control_read_bytes,
            "output": output,
            "manifest_sha256": manifest_digest,
        }
        if logger is not None:
            try:
                HELPER.emit(summary, logger)
                logger.close(output)
            except OSError as exc:
                print(json.dumps({"type": "error", "status": "error", "error": {"kind": "retention_error", "message": str(exc)}}, sort_keys=True), file=sys.stderr, flush=True)
                return_code = 1
        else:
            print(json.dumps(summary, sort_keys=True, separators=(",", ":")), flush=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
