#!/usr/bin/env python3
"""Take one bounded buffered/direct/buffered checkpoint from the live root.

This is deliberately a small production wrapper around the read/comparison
implementation in ``compare-file-reads.py``.  It never mounts, writes to the
compared files, executes target files, requests page-cache eviction or flushes caches.
The live root is always ``/`` and the output is always a new directory below
``/var/tmp/omarchy-nvme-startup-check``.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import importlib.util
import json
import mmap
import os
import re
import signal
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LIVE_ROOT = "/"
OUTPUT_BASE = "/var/tmp/omarchy-nvme-startup-check"
MAX_FILES = 3
MAX_SECONDS = 30.0
MAX_PLANNED_BYTES = 64 * 1024 * 1024
PAGE_BYTES = 4096
BLOCK_BYTES = 1024 * 1024
EXPECTED_ROOT_SOURCE = "/dev/mapper/cryptroot"
PHASES = ("pre-desktop", "post-desktop")
ROOT_CONTEXT: dict[str, Any] = {}
_stop_signal: int | None = None


@dataclass(frozen=True)
class PinnedEntry:
    entry: Any
    identity: tuple[int, ...]
    stat: dict[str, int]
    planned_bytes: int


def _signal_stop(signum: int, _frame: Any) -> None:
    global _stop_signal
    _stop_signal = signum
    raise ProbeError("signal", f"received signal {signum}; stopping", signal=signum)


def _load_comparison_module() -> Any:
    path = Path(__file__).with_name("compare-file-reads.py")
    name = "omarchy_compare_file_reads"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load comparison helper: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


comparison = _load_comparison_module()
ProbeError = comparison.ProbeError


def _read_text(path: str, *, encoding: str = "utf-8") -> str:
    try:
        with open(path, encoding=encoding) as stream:
            return stream.read().strip()
    except OSError as exc:
        raise ProbeError("context_error", f"cannot read {path}: {exc}") from exc


def read_boot_id() -> str:
    value = _read_text("/proc/sys/kernel/random/boot_id", encoding="ascii")
    if not value:
        raise ProbeError("boot_id_error", "the running kernel did not provide a boot ID")
    return value


def read_uptime() -> float:
    value = _read_text("/proc/uptime", encoding="ascii").split()
    try:
        return float(value[0])
    except (IndexError, ValueError) as exc:
        raise ProbeError("context_error", "the running kernel provided an invalid uptime") from exc


def read_root_mount() -> dict[str, str]:
    """Return the root mount from mountinfo without invoking a target command."""

    try:
        with open("/proc/self/mountinfo", encoding="ascii") as stream:
            lines = stream
            for line in lines:
                before, separator, after = line.rstrip("\n").partition(" - ")
                if not separator:
                    continue
                fields = before.split()
                post = after.split()
                if len(fields) < 6 or len(post) < 2:
                    continue
                mountpoint = fields[4].replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")
                if mountpoint == "/":
                    return {"mountpoint": "/", "fstype": post[0], "source": post[1], "options": fields[5]}
    except OSError as exc:
        raise ProbeError("mountinfo_error", f"cannot inspect /proc/self/mountinfo: {exc}") from exc
    raise ProbeError("root_mount_missing", "the running kernel did not report a root mount")


def _initial_user_namespace() -> bool:
    try:
        with open("/proc/self/uid_map", encoding="ascii") as stream:
            rows = [line.split() for line in stream if line.split()]
        if rows != [["0", "0", "4294967295"]]:
            return False
        return os.stat("/proc/self/ns/user").st_ino == os.stat("/proc/1/ns/user").st_ino
    except OSError as exc:
        return False


def check_root_context() -> dict[str, Any]:
    if os.geteuid() != 0:
        raise ProbeError("root_required", "the live-root checkpoint must run as root")
    check_initial_user_namespace()
    return {"euid": os.geteuid(), "egid": os.getegid(), "initial_user_namespace": True}


def check_initial_user_namespace() -> None:
    """Compatibility helper for callers that want only the namespace gate."""

    if not _initial_user_namespace():
        raise ProbeError("initial_user_namespace_required", "the checkpoint requires the initial user namespace")


def check_root_uuid(expected: str, source: str = EXPECTED_ROOT_SOURCE) -> None:
    if not re.fullmatch(r"[0-9A-Fa-f-]{1,128}", expected):
        raise ProbeError("root_uuid_argument", "expected root UUID contains invalid characters")
    link = f"/dev/disk/by-uuid/{expected}"
    try:
        if not os.path.islink(link):
            raise ProbeError("root_uuid_missing", f"root UUID link is unavailable: {link}")
        resolved_link = os.path.realpath(link)
        resolved_source = os.path.realpath(source)
        if resolved_link != resolved_source:
            raise ProbeError("root_uuid_mismatch", f"{link} resolves to {resolved_link}, not {source}")
    except OSError as exc:
        raise ProbeError("root_uuid_error", f"cannot inspect {link}: {exc}") from exc


def guard_live_root(expected_boot_id: str, expected_root_uuid: str, expected_kernel_release: str) -> dict[str, str]:
    """Fail closed unless the supplied identity describes this live root."""

    global ROOT_CONTEXT
    ROOT_CONTEXT = check_root_context()
    actual_boot_id = read_boot_id()
    if actual_boot_id != expected_boot_id:
        raise ProbeError("boot_id_mismatch", f"expected boot ID {expected_boot_id}, observed {actual_boot_id}")
    actual_kernel_release = os.uname().release
    if actual_kernel_release != expected_kernel_release:
        raise ProbeError("kernel_release_mismatch", f"expected kernel {expected_kernel_release}, observed {actual_kernel_release}")
    mount = read_root_mount()
    if mount["fstype"] != "ext4":
        raise ProbeError("root_fstype_mismatch", f"expected an ext4 root, observed {mount['fstype']}")
    if mount["source"] != EXPECTED_ROOT_SOURCE:
        raise ProbeError("root_source_mismatch", f"expected {EXPECTED_ROOT_SOURCE}, observed {mount['source']}")
    check_root_uuid(expected_root_uuid, mount["source"])
    return mount


def _write_fsync(path: str, data: bytes) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        try:
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                view = view[count:]
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ProbeError("retention_error", f"cannot preserve {path}: {exc}") from exc


def create_output() -> str:
    try:
        try:
            os.mkdir(OUTPUT_BASE, 0o700)
        except FileExistsError:
            pass
        base_stat = os.lstat(OUTPUT_BASE)
        expected_owner = os.geteuid()
        if not stat.S_ISDIR(base_stat.st_mode) or stat.S_ISLNK(base_stat.st_mode):
            raise ProbeError("retention_error", f"output base is not a directory: {OUTPUT_BASE}")
        if base_stat.st_uid != expected_owner or stat.S_IMODE(base_stat.st_mode) != 0o700:
            raise ProbeError("retention_error", f"output base must be owner {expected_owner} with mode 0700: {OUTPUT_BASE}")
        output = tempfile.mkdtemp(prefix="checkpoint-", dir=OUTPUT_BASE)
        output_stat = os.lstat(output)
        if output_stat.st_uid != expected_owner or stat.S_IMODE(output_stat.st_mode) != 0o700:
            raise ProbeError("retention_error", f"created output has unexpected owner or mode: {output}")
        return output
    except OSError as exc:
        raise ProbeError("retention_error", f"cannot create output directory: {exc}") from exc


def plan_entries(manifest_bytes: bytes, root_fd: int, deadline: float) -> tuple[list[PinnedEntry], int]:
    """Load and plan no more than three regular, non-symlink-component files."""

    raw_entries = comparison.load_manifest(manifest_bytes)
    if len(raw_entries) > MAX_FILES:
        raise ProbeError("manifest_file_count", f"manifest contains {len(raw_entries)} files; at most {MAX_FILES} are allowed")
    entries: list[Any] = []
    planned = 0
    for relative, expected_size, digest in raw_entries:
        if time.monotonic() >= deadline:
            raise ProbeError("time_limit", "checkpoint runtime limit reached during preflight")
        fd = comparison.open_relative(root_fd, relative, os.O_RDONLY | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ProbeError("not_regular_file", f"manifest path is not a regular file: {relative}")
            if before.st_size != expected_size:
                raise ProbeError("size_mismatch", f"manifest size {expected_size} differs from {before.st_size} for {relative}")
            alignment = comparison.rounded(max(PAGE_BYTES, int(before.st_blksize or PAGE_BYTES)), mmap.PAGESIZE)
            chunk = comparison.rounded(BLOCK_BYTES, alignment)
            direct_requests = sum(
                comparison.rounded(min(chunk, expected_size - offset), alignment)
                for offset in range(0, expected_size, chunk)
            )
            planned += 2 * expected_size + direct_requests
            if planned > MAX_PLANNED_BYTES:
                raise ProbeError("read_budget_exceeded", f"planned reads require {planned} bytes", planned_bytes=planned, max_planned_bytes=MAX_PLANNED_BYTES)
            entry = comparison.Entry(relative, expected_size, digest, alignment, chunk)
            entries.append(PinnedEntry(entry, comparison.stable(before), comparison.stat_record(before), 2 * expected_size + direct_requests))
        finally:
            os.close(fd)
    return entries, planned


def open_pinned(root_fd: int, item: PinnedEntry) -> tuple[int, os.stat_result]:
    fd = comparison.open_relative(root_fd, item.entry.path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode):
            raise ProbeError("not_regular_file", f"manifest path is no longer a regular file: {item.entry.path}")
        if comparison.stable(current) != item.identity:
            raise ProbeError("pinned_identity_changed", f"pinned file identity changed for {item.entry.path}", path=item.entry.path)
        return fd, current
    except BaseException:
        os.close(fd)
        raise


def stat_record_identity(value: dict[str, int]) -> tuple[int, ...]:
    return tuple(int(value[name]) for name in ("st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns"))


def pin_compare_record(record: dict[str, Any], item: PinnedEntry, after_stat: os.stat_result | None = None) -> bool:
    fstat = record.get("fstat")
    if not isinstance(fstat, dict):
        record["pinned_identity_match"] = False
        return False
    values = [fstat.get(name) for name in ("before_buffered", "before_direct", "after_buffered", "after_direct")]
    valid = all(isinstance(value, dict) and stat_record_identity(value) == item.identity for value in values)
    if after_stat is not None:
        record["fstat_after_open"] = comparison.stat_record(after_stat)
        valid = valid and comparison.stable(after_stat) == item.identity
    record["pinned_identity_match"] = valid
    return valid


def partial_captures(output: str, index: int, round_number: int) -> list[str]:
    prefix = os.path.join(output, f"file-{index:03d}-round-{round_number}-")
    return sorted(glob.glob(prefix + "*.partial.bin"))


def _context(phase: str, mount: dict[str, str] | None, command: list[str]) -> dict[str, Any]:
    context: dict[str, Any] = {
        "boot_id": None,
        "uptime_seconds": None,
        "kernel_release": os.uname().release,
        "kernel_root": "/",
        "root": "/",
        "root_context": ROOT_CONTEXT,
        "phase": phase,
        "mount": mount,
        "command": command,
    }
    try:
        context["boot_id"] = read_boot_id()
    except ProbeError:
        pass
    try:
        context["uptime_seconds"] = read_uptime()
    except ProbeError:
        pass
    return context


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="signed, reviewed version-1 JSON manifest")
    parser.add_argument("--expected-boot-id", required=True)
    parser.add_argument("--expected-root-uuid", required=True)
    parser.add_argument("--expected-kernel-release", "--kernel-release", dest="expected_kernel_release", required=True)
    parser.add_argument("--phase", choices=PHASES, required=True, help="checkpoint label; it does not change measurement behavior")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    deadline = started + MAX_SECONDS
    output_path: str | None = None
    logger: Any = None
    manifest_digest: str | None = None
    mount: dict[str, str] | None = None
    planned_total: int | None = None
    result_count = 0
    status = "error"
    return_code = 1
    try:
        signal.signal(signal.SIGTERM, _signal_stop)
        signal.signal(signal.SIGINT, _signal_stop)
        mount = guard_live_root(args.expected_boot_id, args.expected_root_uuid, args.expected_kernel_release)
        output_path = create_output()
        logger = comparison.JsonLogger(output_path)
        try:
            with open(args.manifest, "rb") as stream:
                manifest_bytes = stream.read()
        except OSError as exc:
            raise ProbeError("manifest_error", f"cannot read manifest: {exc}") from exc
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        _write_fsync(os.path.join(output_path, "manifest.json"), manifest_bytes)
        root_fd = os.open(LIVE_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            entries, planned_total = plan_entries(manifest_bytes, root_fd, deadline)
            for index, item in enumerate(entries, 1):
                if time.monotonic() >= deadline:
                    raise ProbeError("time_limit", "checkpoint runtime limit reached")
                pinned_fd, _pinned_stat = open_pinned(root_fd, item)
                os.close(pinned_fd)
                try:
                    record = comparison.compare_entry(root_fd, item.entry, index, 1, output_path, deadline)
                except ProbeError as exc:
                    details = dict(exc.details)
                    details.update({"path": item.entry.path, "partial": partial_captures(output_path, index, 1)})
                    raise ProbeError(exc.kind, exc.message, **details) from exc
                record["pinned_identity"] = list(item.identity)
                after_stat = None
                if record["status"] == "pass":
                    try:
                        after_fd, after_stat = open_pinned(root_fd, item)
                    except ProbeError as exc:
                        record["status"] = "error"
                        record["error"] = {"kind": "pinned_identity_changed", "message": exc.message}
                    else:
                        os.close(after_fd)
                if not pin_compare_record(record, item, after_stat):
                    record["status"] = "error"
                    record["error"] = record.get("error") or {"kind": "pinned_identity_changed", "message": "file identity changed during the checkpoint"}
                    comparison.emit(record, logger)
                    result_count += 1
                    raise ProbeError("pinned_identity_changed", f"checkpoint changed metadata for {item.entry.path}", path=item.entry.path, record=record)
                comparison.emit(record, logger)
                result_count += 1
                if record["status"] != "pass":
                    status = "error" if record["status"] == "error" else "mismatch"
                    kind = "fstat_changed" if record["status"] == "error" else "comparison_mismatch"
                    raise ProbeError(kind, f"checkpoint stopped after {record['status']} for {item.entry.path}", path=item.entry.path, record=record)
        finally:
            os.close(root_fd)
        status = "pass"
        return_code = 0
    except ProbeError as exc:
        if logger is not None:
            comparison.emit({"type": "error", "status": "error", "error": {"kind": exc.kind, "message": exc.message, **exc.details}}, logger)
        else:
            print(json.dumps({"type": "error", "status": "error", "error": {"kind": exc.kind, "message": exc.message, **exc.details}}, sort_keys=True), file=sys.stderr, flush=True)
    except OSError as exc:
        if logger is not None:
            comparison.emit({"type": "error", "status": "error", "error": {"kind": "io_error", "message": str(exc), "errno": exc.errno}}, logger)
        else:
            print(json.dumps({"type": "error", "status": "error", "error": {"kind": "io_error", "message": str(exc), "errno": exc.errno}}, sort_keys=True), file=sys.stderr, flush=True)
    finally:
        if logger is not None:
            try:
                comparison.emit(
                    {
                        "type": "summary",
                        "status": status,
                        "results": result_count,
                        "planned_bytes": planned_total,
                        "max_planned_bytes": MAX_PLANNED_BYTES,
                        "max_seconds": MAX_SECONDS,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "manifest_sha256": manifest_digest,
                        "output": output_path,
                        "context": _context(args.phase, mount, [sys.executable, os.path.abspath(__file__), *(argv if argv is not None else sys.argv[1:])]),
                    },
                    logger,
                )
                logger.close(output_path)
            except (OSError, ProbeError) as exc:
                return_code = 1
                print(json.dumps({"type": "error", "status": "error", "error": {"kind": "retention_error", "message": str(exc)}}, sort_keys=True), file=sys.stderr, flush=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
