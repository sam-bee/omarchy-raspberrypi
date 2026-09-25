#!/usr/bin/env python3
"""Compare buffered and aligned O_DIRECT reads of a mounted filesystem.

The manifest is a version-1 JSON object with ``files`` entries containing a
relative ``path``, byte ``size``, and trusted ``sha256``.  Inputs are opened
read-only with openat-style component checks.  A result and a final summary
are emitted as timestamped JSONL on stdout.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import json
import mmap
import os
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


MAX_ROUNDS = 5
DEFAULT_ROUNDS = 3
MAX_SECONDS = 300.0
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_CAPTURE_FILE_BYTES = 32 * 1024 * 1024
BLOCK_BYTES = 1024 * 1024
PAGE_BYTES = 4096


class ProbeError(Exception):
    def __init__(self, kind: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.details = details


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    sha256: str
    alignment: int
    chunk: int


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonLogger:
    def __init__(self, output: str) -> None:
        try:
            self.fd = os.open(os.path.join(output, "results.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            raise ProbeError("retention_error", f"cannot create results.jsonl: {exc}") from exc

    def write(self, line: str) -> None:
        try:
            view = memoryview((line + "\n").encode())
            while view:
                count = os.write(self.fd, view)
                view = view[count:]
        except OSError as exc:
            raise ProbeError("retention_error", f"cannot write results.jsonl: {exc}") from exc

    def close(self, output: str) -> None:
        try:
            os.fsync(self.fd)
        finally:
            os.close(self.fd)
        directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def emit(record: dict[str, Any], logger: JsonLogger | None = None) -> None:
    record = {"timestamp": timestamp(), **record}
    line = json.dumps(record, sort_keys=True, separators=(",", ":"))
    if logger is not None:
        logger.write(line)
    print(line, flush=True)


def stable(st: os.stat_result) -> tuple[int, ...]:
    return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_uid, st.st_gid, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def stat_record(st: os.stat_result) -> dict[str, int]:
    return {name: int(getattr(st, name)) for name in ("st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns")}


def rounded(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment


def mount_readonly(path: str) -> bool:
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except OSError as exc:
        raise ProbeError("mountinfo_error", f"cannot inspect mount for {path}: {exc}") from exc


def local_fixture(path: str) -> bool:
    if os.geteuid() == 0:
        return False
    resolved = os.path.realpath(path)
    if not any(resolved == base or resolved.startswith(base + "/") for base in ("/tmp", "/var/tmp")):
        return False
    try:
        return os.stat(resolved).st_uid == os.geteuid()
    except OSError:
        return False


def check_mount(path: str, allow_fixture: bool) -> None:
    if mount_readonly(path):
        return
    if allow_fixture and local_fixture(path):
        return
    raise ProbeError("target_mount_not_readonly", f"target path is on a writable mount: {os.path.realpath(path)}")


def check_fd_mount(fd: int, path: str, allow_fixture: bool) -> None:
    try:
        readonly = bool(os.fstatvfs(fd).f_flag & os.ST_RDONLY)
    except OSError as exc:
        raise ProbeError("mountinfo_error", f"cannot inspect opened mount for {path}: {exc}") from exc
    if readonly:
        return
    if allow_fixture and local_fixture(path):
        return
    raise ProbeError("target_mount_not_readonly", f"opened target path is on a writable mount: {os.path.realpath(path)}")


def validate_relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or value.startswith("/"):
        raise ProbeError("manifest_path", "manifest paths must be non-empty relative paths")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ProbeError("manifest_path", f"manifest path is not a simple relative path: {value!r}")
    return value


def load_manifest(source: str | bytes) -> list[tuple[str, int, str]]:
    try:
        if isinstance(source, bytes):
            value = json.loads(source)
        else:
            with open(source, encoding="utf-8") as stream:
                value = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProbeError("manifest_error", f"cannot read manifest: {exc}") from exc
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("files"), list):
        raise ProbeError("manifest_schema", "manifest must be an object with version 1 and a files array")
    result: list[tuple[str, int, str]] = []
    seen: set[str] = set()
    for item in value["files"]:
        if not isinstance(item, dict):
            raise ProbeError("manifest_schema", "each manifest file must be an object")
        relative = validate_relative(item.get("path"))
        size = item.get("size")
        digest = item.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > MAX_FILE_BYTES:
            raise ProbeError("manifest_size", f"invalid or oversized manifest size for {relative!r}")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest):
            raise ProbeError("manifest_sha256", f"invalid SHA-256 for {relative!r}")
        if relative in seen:
            raise ProbeError("manifest_duplicate", f"duplicate manifest path: {relative!r}")
        seen.add(relative)
        result.append((relative, size, digest.lower()))
    if not result:
        raise ProbeError("manifest_schema", "manifest files array must not be empty")
    return result


def open_relative(root_fd: int, relative: str, flags: int) -> int:
    parts = relative.split("/")
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
            os.close(current)
            current = next_fd
        return os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
    except OSError as exc:
        raise ProbeError("open_error", f"cannot open {relative!r} read-only: {exc}", errno=exc.errno) from exc
    finally:
        os.close(current)


def open_direct(root_fd: int, relative: str) -> int:
    direct = getattr(os, "O_DIRECT", None)
    if direct is None:
        raise ProbeError("odirect_unavailable", "this platform has no O_DIRECT")
    return open_relative(root_fd, relative, os.O_RDONLY | os.O_NONBLOCK | int(direct))


def read_buffered(fd: int, offset: int, length: int) -> bytes:
    try:
        data = os.pread(fd, length, offset)
    except OSError as exc:
        raise ProbeError("buffered_read_error", f"buffered read failed at {offset}: {exc}", errno=exc.errno) from exc
    if len(data) != length:
        raise ProbeError("buffered_short_read", f"buffered read returned {len(data)} of {length} bytes", offset=offset)
    return data


def load_pread() -> Any:
    try:
        pread = ctypes.CDLL(None, use_errno=True).pread
        pread.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_longlong]
        pread.restype = ctypes.c_ssize_t
        return pread
    except (AttributeError, OSError) as exc:
        raise ProbeError("pread_unavailable", f"libc pread is unavailable: {exc}") from exc


def read_direct(pread: Any, fd: int, buffer: mmap.mmap, offset: int, logical: int, size: int, alignment: int) -> tuple[bytes, int]:
    request = rounded(logical, alignment)
    base = ctypes.addressof(ctypes.c_char.from_buffer(buffer))
    aligned = rounded(base, alignment)
    ctypes.set_errno(0)
    returned = int(pread(fd, ctypes.c_void_p(aligned), ctypes.c_size_t(request), ctypes.c_longlong(offset)))
    if returned < 0:
        error = ctypes.get_errno()
        raise ProbeError("direct_read_error", f"O_DIRECT read failed at {offset}: [{error}] {os.strerror(error)}", errno=error)
    if returned != logical:
        raise ProbeError("direct_eof_tail", f"O_DIRECT returned {returned} of {logical} logical bytes", offset=offset, requested=request, eof_tail=(offset + logical == size))
    return ctypes.string_at(aligned, returned), request


class Captures:
    def __init__(self, output: str, index: int, round_number: int) -> None:
        self.output = output
        self.prefix = os.path.join(output, f"file-{index:03d}-round-{round_number}")
        self.names = ("buffered1", "direct", "buffered2")
        self.buffers = [io.BytesIO(), io.BytesIO(), io.BytesIO()]

    def write(self, stream: int, data: bytes) -> None:
        self.buffers[stream].write(data)

    def finish(self, keep: bool, partial: bool = False) -> list[str]:
        if not keep:
            for stream in self.buffers:
                stream.close()
            return []
        final = []
        suffix = ".partial" if partial else ""
        for name, stream in zip(self.names, self.buffers):
            target = f"{self.prefix}-{name}{suffix}.bin"
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
                try:
                    view = memoryview(stream.getvalue())
                    try:
                        offset = 0
                        while offset < len(view):
                            offset += os.write(fd, view[offset:])
                    finally:
                        view.release()
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                raise ProbeError("retention_error", f"cannot retain {target}: {exc}") from exc
            stream.close()
            final.append(target)
        dir_fd = os.open(self.output, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return final


def compare_entry(root_fd: int, entry: Entry, index: int, round_number: int, output: str, deadline: float) -> dict[str, Any]:
    buffered_fd = direct_fd = -1
    mm: mmap.mmap | None = None
    captures: Captures | None = None
    try:
        buffered_fd = open_relative(root_fd, entry.path, os.O_RDONLY | os.O_NONBLOCK)
        direct_fd = open_direct(root_fd, entry.path)
        before_buffered = os.fstat(buffered_fd)
        before_direct = os.fstat(direct_fd)
        if not stat.S_ISREG(before_buffered.st_mode) or not stat.S_ISREG(before_direct.st_mode):
            raise ProbeError("not_regular_file", f"manifest path is not a regular file: {entry.path}")
        if stable(before_buffered) != stable(before_direct):
            raise ProbeError("inode_mismatch", f"buffered and direct descriptors differ for {entry.path}")
        if before_buffered.st_size != entry.size:
            raise ProbeError("size_mismatch", f"manifest size {entry.size} differs from {before_buffered.st_size} for {entry.path}")
        if entry.size:
            mm = mmap.mmap(-1, entry.chunk + entry.alignment)
        captures = Captures(output, index, round_number)
        pread = load_pread()
        hashes = [hashlib.sha256(), hashlib.sha256(), hashlib.sha256()]
        requested_direct = 0
        offset = 0
        while offset < entry.size:
            if time.monotonic() >= deadline:
                raise ProbeError("time_limit", "probe runtime limit reached")
            logical = min(entry.chunk, entry.size - offset)
            first = read_buffered(buffered_fd, offset, logical)
            captures.write(0, first)
            direct, requested = read_direct(pread, direct_fd, mm, offset, logical, entry.size, entry.alignment)
            captures.write(1, direct)
            second = read_buffered(buffered_fd, offset, logical)
            captures.write(2, second)
            for digest, data in zip(hashes, (first, direct, second)):
                digest.update(data)
            requested_direct += requested
            offset += logical
        after_buffered = os.fstat(buffered_fd)
        after_direct = os.fstat(direct_fd)
        stable_file = stable(before_buffered) == stable(after_buffered) == stable(after_direct)
        digests = [digest.hexdigest() for digest in hashes]
        expected = all(digest == entry.sha256 for digest in digests)
        agreement = digests[0] == digests[1] == digests[2]
        status = "pass" if stable_file and agreement and expected else ("mismatch" if stable_file else "error")
        retained = captures.finish(status != "pass")
        captures = None
        return {
            "type": "result", "status": status, "path": entry.path, "round": round_number, "size": entry.size,
            "alignment": entry.alignment, "buffered1": {"sha256": digests[0], "bytes": entry.size},
            "direct": {"sha256": digests[1], "bytes": entry.size, "requested_bytes": requested_direct, "eof_tail_validated": True},
            "buffered2": {"sha256": digests[2], "bytes": entry.size},
            "agreement": agreement, "expected_sha256": entry.sha256, "expected_match": expected,
            "same_inode": stable(before_buffered)[:2] == stable(before_direct)[:2], "fstat_stable": stable_file,
            "fstat": {"before_buffered": stat_record(before_buffered), "before_direct": stat_record(before_direct), "after_buffered": stat_record(after_buffered), "after_direct": stat_record(after_direct)},
            "error": None if stable_file else {"kind": "fstat_changed", "message": "file metadata changed during the read"},
            "retained": retained,
        }
    except ProbeError:
        if captures is not None:
            try:
                captures.finish(True, partial=True)
            except (OSError, ProbeError):
                pass
            captures = None
        raise
    except OSError as exc:
        if captures is not None:
            try:
                captures.finish(True, partial=True)
            except (OSError, ProbeError):
                pass
            captures = None
        raise ProbeError("io_error", str(exc), errno=exc.errno) from exc
    finally:
        if mm is not None:
            mm.close()
        if direct_fd >= 0:
            os.close(direct_fd)
        if buffered_fd >= 0:
            os.close(buffered_fd)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="mounted filesystem root containing manifest paths")
    parser.add_argument("--manifest", required=True, help="version-1 JSON manifest")
    parser.add_argument("--output", required=True, help="new private directory for mismatch evidence")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--max-seconds", type=float, default=MAX_SECONDS)
    parser.add_argument("--max-total-bytes", type=int, default=MAX_TOTAL_BYTES, help="lower the hard 2 GiB aggregate cap")
    parser.add_argument("--allow-writable-fixture", action="store_true", help="allow a caller-owned /tmp or /var/tmp fixture (never as root)")
    args = parser.parse_args(argv)
    if not 1 <= args.rounds <= MAX_ROUNDS:
        parser.error("--rounds must be between 1 and 5")
    if not 0 < args.max_seconds <= MAX_SECONDS:
        parser.error("--max-seconds must be greater than zero and no more than 300")
    if not 0 < args.max_total_bytes <= MAX_TOTAL_BYTES:
        parser.error("--max-total-bytes must be positive and no more than 2 GiB")
    return args


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    deadline = started + args.max_seconds
    results = 0
    status = "error"
    output_path = os.path.abspath(args.output)
    root_fd = -1
    logger: JsonLogger | None = None
    manifest_digest: str | None = None
    return_code = 1
    try:
        if args.allow_writable_fixture and os.geteuid() == 0:
            raise ProbeError("fixture_requires_nonroot", "--allow-writable-fixture is unavailable to root")
        root = os.path.realpath(args.root)
        if not os.path.isdir(root):
            raise ProbeError("root_error", f"--root is not a directory: {args.root}")
        check_mount(root, args.allow_writable_fixture)
        output_real = os.path.realpath(output_path)
        if os.path.commonpath((root, output_real)) == root:
            raise ProbeError("output_inside_root", "--output must be outside --root")
        if os.path.lexists(output_path):
            raise ProbeError("output_exists", "--output must name a new directory")
        try:
            with open(args.manifest, "rb") as manifest_file:
                manifest_bytes = manifest_file.read()
        except OSError as exc:
            raise ProbeError("manifest_error", f"cannot read manifest: {exc}") from exc
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        entries_raw = load_manifest(manifest_bytes)
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        check_fd_mount(root_fd, root, args.allow_writable_fixture)
        entries: list[Entry] = []
        planned = 0
        for index, (relative, expected_size, digest) in enumerate(entries_raw, 1):
            if time.monotonic() >= deadline:
                raise ProbeError("time_limit", "probe runtime limit reached during preflight")
            candidate = os.path.join(root, relative)
            check_mount(candidate, args.allow_writable_fixture)
            buffered_fd = open_relative(root_fd, relative, os.O_RDONLY | os.O_NONBLOCK)
            try:
                check_fd_mount(buffered_fd, candidate, args.allow_writable_fixture)
                st = os.fstat(buffered_fd)
                if not stat.S_ISREG(st.st_mode):
                    raise ProbeError("not_regular_file", f"manifest path is not a regular file: {relative}")
                if st.st_size != expected_size:
                    raise ProbeError("size_mismatch", f"manifest size {expected_size} differs from {st.st_size} for {relative}")
                alignment = rounded(max(PAGE_BYTES, int(st.st_blksize or PAGE_BYTES)), mmap.PAGESIZE)
                chunk = rounded(BLOCK_BYTES, alignment)
                direct_requests = sum(rounded(min(chunk, expected_size - offset), alignment) for offset in range(0, expected_size, chunk))
                planned += args.rounds * (2 * expected_size + direct_requests)
                entries.append(Entry(relative, expected_size, digest, alignment, chunk))
            finally:
                os.close(buffered_fd)
        if planned > args.max_total_bytes:
            raise ProbeError("read_budget_exceeded", f"planned reads require {planned} bytes", planned_bytes=planned, max_total_bytes=args.max_total_bytes)
        if any(entry.size > MAX_CAPTURE_FILE_BYTES or entry.size * 3 > MAX_OUTPUT_BYTES for entry in entries):
            raise ProbeError("capture_budget_exceeded", "a complete three-stream capture would exceed the per-file evidence bound")
        os.mkdir(output_path, 0o700)
        os.chmod(output_path, 0o700)
        try:
            fd = os.open(os.path.join(output_path, "manifest.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            try:
                view = memoryview(manifest_bytes)
                while view:
                    count = os.write(fd, view)
                    view = view[count:]
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise ProbeError("retention_error", f"cannot preserve manifest: {exc}") from exc
        logger = JsonLogger(output_path)
        for index, entry in enumerate(entries, 1):
            for round_number in range(1, args.rounds + 1):
                if time.monotonic() >= deadline:
                    raise ProbeError("time_limit", "probe runtime limit reached")
                record = compare_entry(root_fd, entry, index, round_number, output_path, deadline)
                emit(record, logger)
                results += 1
                if record["status"] != "pass":
                    status = "error" if record["status"] == "error" else "mismatch"
                    kind = "fstat_changed" if record["status"] == "error" else "comparison_mismatch"
                    raise ProbeError(kind, f"read streams did not produce a stable manifest match for {entry.path}")
        status = "pass"
        return_code = 0
    except ProbeError as exc:
        emit({"type": "error", "status": "error", "error": {"kind": exc.kind, "message": exc.message, **exc.details}}, logger)
        return_code = 1
    except OSError as exc:
        emit({"type": "error", "status": "error", "error": {"kind": "io_error", "message": str(exc), "errno": exc.errno}}, logger)
        return_code = 1
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        context: dict[str, Any] = {"manifest_sha256": manifest_digest, "boot_id": None, "kernel_release": os.uname().release}
        try:
            with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as boot_file:
                context["boot_id"] = boot_file.read().strip()
        except OSError:
            pass
        try:
            emit({"type": "summary", "status": status, "results": results, "rounds": args.rounds, "elapsed_seconds": round(time.monotonic() - started, 3), "max_total_bytes": args.max_total_bytes, "max_output_bytes": MAX_OUTPUT_BYTES, "output": output_path, "context": context}, logger)
        finally:
            if logger is not None:
                try:
                    logger.close(output_path)
                except OSError as exc:
                    return_code = 1
                    print(json.dumps({"timestamp": timestamp(), "type": "error", "status": "error", "error": {"kind": "retention_error", "message": f"cannot fsync results.jsonl: {exc}"}}, sort_keys=True, separators=(",", ":")), file=sys.stderr, flush=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
