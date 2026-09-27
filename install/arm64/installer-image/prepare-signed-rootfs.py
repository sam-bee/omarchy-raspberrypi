#!/usr/bin/python3
"""Prepare a signed Arch Linux ARM rootfs for the offline image builder.

This step deliberately has a small boundary.  It accepts files that have
already been downloaded, verifies the detached signature with the caller's
trusted Arch Linux ARM keyring, hashes the archive, and extracts it into a
new staging directory with ``bsdtar``.  It never installs packages, contacts a
Pi, or writes a block device.  The normal command-line entry point is intended
for a root, native-aarch64 build host so archive ownership and permissions can
be retained exactly.

The output layout is::

    OUTPUT/
      rootfs/                 # extracted archive, suitable for assemble-image
      rootfs-manifest.json    # provenance only; not part of rootfs/
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import posixpath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Sequence


EXPECTED_FINGERPRINT_LENGTH = 40
SHA256_LENGTH = 64
OUTPUT_ROOT_NAME = "rootfs"
MANIFEST_NAME = "rootfs-manifest.json"
HASH_BLOCK_BYTES = 4 * 1024 * 1024


class RootfsPreparationError(RuntimeError):
    """Raised when a rootfs cannot be prepared without violating a guard."""


@dataclass(frozen=True, slots=True)
class ArchiveInspection:
    """Non-secret facts retained in the output manifest."""

    member_count: int
    regular_file_bytes: int


def _absolute_lexical(path: Path) -> Path:
    """Return an absolute path without resolving symlinks."""

    if path.is_absolute():
        return Path(os.path.normpath(os.fspath(path)))
    return Path(os.path.normpath(os.path.join(os.getcwd(), os.fspath(path))))


def _reject_symlink_components(path: Path, *, include_leaf: bool = True) -> None:
    """Reject symlink components so later path checks cannot be redirected."""

    absolute = _absolute_lexical(path)
    current = Path(absolute.anchor)
    components = absolute.parts[1:]
    if not include_leaf:
        components = components[:-1]
    for component in components:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise RootfsPreparationError(f"refusing symlink path component: {current}")


def _require_regular_file(path: Path, *, name: str, nonempty: bool = True) -> Path:
    """Validate an input file without following a symlink at its leaf."""

    candidate = _absolute_lexical(path)
    _reject_symlink_components(candidate, include_leaf=True)
    try:
        info = candidate.lstat()
    except FileNotFoundError as exc:
        raise RootfsPreparationError(f"{name} does not exist") from exc
    if stat.S_ISLNK(info.st_mode):
        raise RootfsPreparationError(f"{name} must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise RootfsPreparationError(f"{name} must be a regular file")
    if nonempty and info.st_size == 0:
        raise RootfsPreparationError(f"{name} is empty")
    return candidate


def _validate_fingerprint(value: str) -> str:
    normalized = value.strip().upper()
    if len(normalized) != EXPECTED_FINGERPRINT_LENGTH or any(
        character not in "0123456789ABCDEF" for character in normalized
    ):
        raise RootfsPreparationError("expected signer fingerprint must be 40 hexadecimal characters")
    return normalized


def _validate_sha256(value: str, *, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise RootfsPreparationError(f"{name} must be a 64-character hexadecimal SHA-256")
    return normalized


def _protected_output_path(path: Path) -> str | None:
    """Return a protected tree name, if an output would be unsafe there."""

    protected = (Path("/dev"), Path("/proc"), Path("/sys"), Path("/run"), Path("/boot"))
    for tree in protected:
        try:
            path.relative_to(tree)
        except ValueError:
            continue
        return str(tree)
    return None


def validate_output_directory(path: Path) -> Path:
    """Validate a new output directory before creating any files."""

    candidate = _absolute_lexical(path)
    if candidate == Path("/"):
        raise RootfsPreparationError("refusing the host root as the output directory")
    protected = _protected_output_path(candidate)
    if protected is not None:
        raise RootfsPreparationError(f"refusing output under {protected}")
    if os.path.lexists(candidate):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RootfsPreparationError("output directory must not be a symlink")
        if stat.S_ISBLK(info.st_mode) or stat.S_ISCHR(info.st_mode):
            raise RootfsPreparationError("refusing a device node as the output directory")
        raise RootfsPreparationError(f"refusing to overwrite existing output directory: {candidate}")

    parent = candidate.parent
    _reject_symlink_components(parent, include_leaf=True)
    try:
        parent_info = parent.lstat()
    except FileNotFoundError as exc:
        raise RootfsPreparationError("output directory parent does not exist") from exc
    if not stat.S_ISDIR(parent_info.st_mode):
        raise RootfsPreparationError("output directory parent is not a directory")
    return candidate


def _sha256_file(path: Path) -> tuple[str, int]:
    """Hash a regular file through a no-follow descriptor."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RootfsPreparationError(f"cannot open archive for hashing: {exc.strerror}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RootfsPreparationError("archive changed into a non-regular file")
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            for block in iter(lambda: stream.read(HASH_BLOCK_BYTES), b""):
                digest.update(block)
                total += len(block)
        return digest.hexdigest(), total
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def verify_detached_signature(
    archive: Path,
    signature: Path,
    keyring: Path,
    expected_fingerprint: str,
    *,
    gpgv_command: str = "gpgv",
) -> str:
    """Verify a detached signature and return its one valid signer."""

    archive = _require_regular_file(archive, name="rootfs archive")
    signature = _require_regular_file(signature, name="rootfs detached signature")
    keyring = _require_regular_file(keyring, name="trusted keyring")
    expected = _validate_fingerprint(expected_fingerprint)
    try:
        result = subprocess.run(
            [gpgv_command, "--status-fd", "1", "--keyring", os.fspath(keyring), os.fspath(signature), os.fspath(archive)],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RootfsPreparationError(f"could not run gpgv: {exc.strerror}") from exc
    if result.returncode != 0:
        raise RootfsPreparationError("gpgv rejected the rootfs signature")
    valid_signers: list[str] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0] == "[GNUPG:]" and fields[1] == "VALIDSIG":
            valid_signers.append(fields[2].upper())
    if len(valid_signers) != 1:
        raise RootfsPreparationError("gpgv did not produce exactly one valid signer")
    signer = _validate_fingerprint(valid_signers[0])
    if signer != expected:
        raise RootfsPreparationError("rootfs signature signer differs from the expected fingerprint")
    return signer


def _safe_member_path(name: str, *, field_name: str = "archive member") -> str:
    if not name or "\x00" in name:
        raise RootfsPreparationError(f"{field_name} has an invalid path")
    if name.startswith("/"):
        raise RootfsPreparationError(f"{field_name} is absolute")
    normalized = posixpath.normpath(name)
    if normalized == ".." or normalized.startswith("../"):
        raise RootfsPreparationError(f"{field_name} escapes the rootfs")
    return normalized


def _safe_link_target(member_name: str, target: str) -> None:
    if not target or "\x00" in target:
        raise RootfsPreparationError("archive link has an invalid target")
    if target.startswith("/"):
        # Absolute links are interpreted relative to the installed root at
        # runtime and are normal in Linux root filesystems.
        return
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(member_name), target))
    if joined == ".." or joined.startswith("../"):
        raise RootfsPreparationError("archive link escapes the rootfs")


def inspect_archive(archive: Path) -> ArchiveInspection:
    """Check member paths and reject special files before extraction."""

    member_count = 0
    regular_file_bytes = 0
    try:
        stream = tarfile.open(archive, mode="r:*")
    except (OSError, tarfile.TarError) as exc:
        raise RootfsPreparationError("cannot read rootfs archive") from exc
    with stream:
        for member in stream:
            member_count += 1
            member_name = _safe_member_path(member.name)
            if member.isreg():
                regular_file_bytes += member.size
            elif member.issym():
                _safe_link_target(member_name, member.linkname)
            elif member.islnk():
                _safe_member_path(member.linkname, field_name="archive hard-link target")
            elif member.isdir():
                continue
            else:
                raise RootfsPreparationError("rootfs archive contains a device, FIFO, or other special file")
    if member_count == 0:
        raise RootfsPreparationError("rootfs archive is empty")
    return ArchiveInspection(member_count=member_count, regular_file_bytes=regular_file_bytes)


def extract_rootfs(archive: Path, destination: Path, *, bsdtar_command: str = "bsdtar") -> None:
    """Extract a preflighted archive while preserving permissions and owners."""

    try:
        result = subprocess.run(
            [
                bsdtar_command,
                "-xpf",
                os.fspath(archive),
                "-C",
                os.fspath(destination),
                "--numeric-owner",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RootfsPreparationError(f"could not run bsdtar: {exc.strerror}") from exc
    if result.returncode != 0:
        raise RootfsPreparationError("bsdtar failed to extract the rootfs archive")


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".rootfs-manifest.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _require_build_host(*, require_native_aarch64: bool, require_root: bool) -> str:
    machine = platform.machine().lower()
    if require_native_aarch64 and machine != "aarch64":
        raise RootfsPreparationError("rootfs preparation must run on a native aarch64 build host")
    if require_root and os.geteuid() != 0:
        raise RootfsPreparationError("rootfs extraction must run as root to preserve ownership")
    return machine


def prepare_rootfs(
    archive: Path,
    signature: Path,
    keyring: Path,
    expected_fingerprint: str,
    output_directory: Path,
    *,
    expected_archive_sha256: str | None = None,
    gpgv_command: str = "gpgv",
    bsdtar_command: str = "bsdtar",
    require_native_aarch64: bool = True,
    require_root: bool = True,
) -> dict[str, Any]:
    """Verify and extract one archive into a newly created staging directory."""

    machine = _require_build_host(require_native_aarch64=require_native_aarch64, require_root=require_root)
    archive_path = _require_regular_file(archive, name="rootfs archive")
    signature_path = _require_regular_file(signature, name="rootfs detached signature")
    keyring_path = _require_regular_file(keyring, name="trusted keyring")
    output_path = validate_output_directory(output_directory)
    expected_fingerprint = _validate_fingerprint(expected_fingerprint)
    if expected_archive_sha256 is not None:
        expected_archive_sha256 = _validate_sha256(expected_archive_sha256, name="expected archive SHA-256")

    signer = verify_detached_signature(
        archive_path,
        signature_path,
        keyring_path,
        expected_fingerprint,
        gpgv_command=gpgv_command,
    )
    archive_sha256, archive_bytes = _sha256_file(archive_path)
    if expected_archive_sha256 is not None and archive_sha256 != expected_archive_sha256:
        raise RootfsPreparationError("rootfs archive SHA-256 differs from the expected hash")
    inspection = inspect_archive(archive_path)

    try:
        output_path.mkdir(mode=0o700)
        rootfs_path = output_path / OUTPUT_ROOT_NAME
        rootfs_path.mkdir(mode=0o755)
        extract_rootfs(archive_path, rootfs_path, bsdtar_command=bsdtar_command)
        if not any(rootfs_path.iterdir()):
            raise RootfsPreparationError("bsdtar produced an empty rootfs")
        manifest = {
            "schema_version": 1,
            "source": {
                "archive_name": archive_path.name,
                "archive_bytes": archive_bytes,
                "archive_sha256": archive_sha256,
                "signature_name": signature_path.name,
                "signer_fingerprint": signer,
            },
            "archive": {
                "member_count": inspection.member_count,
                "regular_file_bytes": inspection.regular_file_bytes,
            },
            "staging": {"rootfs_directory": OUTPUT_ROOT_NAME},
            "build_host": {"architecture": machine, "native_aarch64": machine == "aarch64"},
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _write_manifest(output_path / MANIFEST_NAME, manifest)
    except BaseException:
        try:
            if output_path.is_dir() and not output_path.is_symlink():
                shutil.rmtree(output_path)
        except OSError:
            pass
        raise
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", "--rootfs-archive", dest="archive", type=Path, required=True)
    parser.add_argument("--signature", "--archive-signature", dest="signature", type=Path, required=True)
    parser.add_argument("--keyring", type=Path, required=True, help="trusted Arch Linux ARM keyring file")
    parser.add_argument(
        "--signer-fingerprint",
        "--expected-fingerprint",
        dest="expected_fingerprint",
        required=True,
        help="expected full OpenPGP signer fingerprint",
    )
    parser.add_argument("--output-dir", "--output", dest="output_directory", type=Path, required=True)
    parser.add_argument(
        "--archive-sha256",
        "--expected-archive-sha256",
        dest="expected_archive_sha256",
        help="optional expected archive SHA-256; always recorded in the manifest",
    )
    parser.add_argument("--gpgv", default="gpgv", help=argparse.SUPPRESS)
    parser.add_argument("--bsdtar", default="bsdtar", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = prepare_rootfs(
            args.archive,
            args.signature,
            args.keyring,
            args.expected_fingerprint,
            args.output_directory,
            expected_archive_sha256=args.expected_archive_sha256,
            gpgv_command=args.gpgv,
            bsdtar_command=args.bsdtar,
        )
    except (RootfsPreparationError, OSError, tarfile.TarError) as exc:
        print(f"prepare-signed-rootfs: error: {exc}", file=sys.stderr)
        return 2
    source = manifest["source"]
    print(
        f"prepared {args.output_directory}; "
        f"archive sha256={source['archive_sha256']} signer={source['signer_fingerprint']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
