#!/usr/bin/python3
"""Validation and command planning for the packaged ARM Omarchy runtime.

The Pi does not have a trusted ARM Omarchy repository yet.  A packaged target
therefore updates from an explicitly reviewed local candidate directory.  This
module is deliberately side-effect free apart from reading that directory: the
durable worker in :mod:`update.py` owns the transaction and its journal.

Candidate layout (schema 1)::

    current/
      candidate.json                 # updater wrapper, or pair.json from the builder
      pair.json                       # native build-runtime-packages.py output
      packages/<two signed or explicitly reviewed archives>  # wrapper layout
      <builder pair archives>                                # pair.json layout
      previous/<rollback archives>
      previous.json                   # records the retained previous pair
      source/<clean pinned source tree>
      source.tar                      # deterministic Git archive for the source tree
      source/.omarchy-pi-source-commit  # exact source revision bound to the pair

``candidate.json`` records the exact pair, archive digests, source revision and
rollback archives.  The pair must be either ``omarchy`` +
``omarchy-settings`` or their matching ``-dev`` variants.  The updater never
falls back to an upstream package feed when this manifest is absent.

The native runtime package builder emits ``pair.json`` and archives but does
not decide which prior pair is retained for rollback.  The image/candidate
assembly step may use that pair directly, provided it adds ``source/`` and a
``previous.json`` record with the same package-record shape before publishing
the candidate directory.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
from typing import Any, Callable, Iterable


DEFAULT_CANDIDATE_ROOT = Path("/var/lib/omarchy-pi-updates/candidates/current")
ROLLBACK_ROOTS = (Path("/usr/share/omarchy-pi/rollback"), Path("/var/cache/pacman/pkg"))
RUNTIME_ROOT = Path("/usr/share/omarchy")
PACKAGED_MARKER = ".omarchy-pi-packaged.json"
REVISION = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
PACKAGE_PAIRS = {
    "stable": ("omarchy", "omarchy-settings"),
    "dev": ("omarchy-dev", "omarchy-settings-dev"),
}


class CandidateError(RuntimeError):
    """The candidate is absent, malformed, or incomplete for safe review."""


@dataclass(frozen=True)
class CandidatePackage:
    name: str
    version: str
    architecture: str
    archive: Path
    sha256: str
    signature: str
    signature_file: Path | None = None


@dataclass(frozen=True)
class Candidate:
    root: Path
    manifest: Path
    channel: str
    source_revision: str
    source_sha256: str
    source_tree: Path
    source_archive: Path
    packages: tuple[CandidatePackage, ...]
    previous_packages: tuple[CandidatePackage, ...]

    @property
    def package_names(self) -> tuple[str, str]:
        return tuple(package.name for package in self.packages)  # type: ignore[return-value]


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_child(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise CandidateError(f"{label} must be a relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        raise CandidateError(f"{label} must stay inside the candidate directory")
    path = root / relative
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CandidateError(f"{label} is unavailable: {path}") from exc
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise CandidateError(f"{label} escapes the candidate directory") from exc
    return path


def _record_package(root: Path, record: object, label: str) -> CandidatePackage:
    if not isinstance(record, dict):
        raise CandidateError(f"{label} must be an object")
    name = record.get("name")
    version = record.get("version")
    architecture = record.get("architecture")
    filename = record.get("filename")
    sha256 = record.get("sha256")
    signature = record.get("signature", "required")
    if not all(isinstance(value, str) and value.strip() for value in (name, version, architecture, filename, sha256)):
        raise CandidateError(f"{label} is missing package metadata")
    if architecture != "aarch64":
        raise CandidateError(f"{label} is not an aarch64 package")
    if not SHA256.fullmatch(sha256):
        raise CandidateError(f"{label} has an invalid SHA-256")
    if signature not in {"required", "optional"}:
        raise CandidateError(f"{label} has an unsupported signature policy: {signature}")
    archive = _safe_child(root, filename, f"{label} archive")
    if archive.is_symlink() or not archive.is_file():
        raise CandidateError(f"{label} archive is not a regular file: {archive}")
    actual = _digest(archive)
    if actual != sha256:
        raise CandidateError(f"{label} archive digest differs from its manifest")
    signature_path = None
    if signature == "required":
        signature_file = record.get("signature_file")
        signature_path = _safe_child(root, signature_file, f"{label} signature") if signature_file else Path(str(archive) + ".sig")
        if signature_path.is_symlink() or not signature_path.is_file():
            raise CandidateError(f"{label} requires a detached package signature: {signature_path}")
    return CandidatePackage(name, version, architecture, archive, sha256, signature, signature_path)


def _archive_member_bytes(archive: Path, name: str) -> bytes:
    try:
        result = subprocess.run(
            ["/usr/bin/bsdtar", "-xOf", str(archive), "--", name],
            check=False, capture_output=True,
        )
    except OSError as exc:
        raise CandidateError("cannot inspect native package archives: bsdtar is unavailable") from exc
    if result.returncode != 0:
        raise CandidateError(f"native package archive has no readable {name}: {archive.name}")
    return result.stdout


def _archive_members(archive: Path) -> set[str]:
    try:
        result = subprocess.run(
            ["/usr/bin/bsdtar", "-tf", str(archive)],
            check=False, capture_output=True, text=True,
        )
    except OSError as exc:
        raise CandidateError("cannot inspect native package archives: bsdtar is unavailable") from exc
    if result.returncode != 0:
        raise CandidateError(f"native package archive cannot be listed: {archive.name}")
    return {line.rstrip("/") for line in result.stdout.splitlines() if line.rstrip("/") not in {"", "."}}


def _pkginfo(data: bytes, archive: Path) -> dict[str, list[str]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CandidateError(f"native package metadata is not UTF-8: {archive.name}") from exc
    fields: dict[str, list[str]] = {}
    for line in text.splitlines():
        if " = " not in line:
            continue
        key, value = line.split(" = ", 1)
        fields.setdefault(key, []).append(value)
    return fields


def _pacman_owner(output: str) -> str | None:
    """Extract the package name from pacman's quiet owner query."""

    fields = output.split()
    if len(fields) == 1 and fields[0] in set(PACKAGE_PAIRS["stable"] + PACKAGE_PAIRS["dev"]):
        return fields[0]
    return None


def _validate_native_package(
    package: CandidatePackage,
    channel: str,
    *,
    source_revision: str | None = None,
    source_sha256: str | None = None,
    retained: bool = False,
) -> None:
    """Verify the archive's native pacman metadata and runtime provenance."""

    expected_names = PACKAGE_PAIRS[channel]
    expected_filename = f"{package.name}-{package.version}-{package.architecture}.pkg.tar.zst"
    if package.archive.name != expected_filename and not (
        retained and package.archive.name.endswith("-" + expected_filename)
    ):
        raise CandidateError(f"native package filename disagrees with metadata: {package.archive.name}")
    members = _archive_members(package.archive)
    metadata = _pkginfo(_archive_member_bytes(package.archive, ".PKGINFO"), package.archive)
    if (
        metadata.get("pkgname") != [package.name]
        or metadata.get("pkgver") != [package.version]
        or metadata.get("arch") != [package.architecture]
    ):
        raise CandidateError(f"native package metadata disagrees with its manifest: {package.archive.name}")
    if package.name == expected_names[0] and f"{expected_names[1]}={package.version}" not in metadata.get("depend", []):
        raise CandidateError(f"{package.name} does not depend on the exact settings pair")
    if package.name != expected_names[0] and package.name != expected_names[1]:
        raise CandidateError(f"native package is outside the selected {channel} runtime pair: {package.name}")
    if package.name != expected_names[0]:
        return
    source_marker = "usr/share/omarchy/.omarchy-pi-source-commit"
    packaged_marker = "usr/share/omarchy/.omarchy-pi-packaged.json"
    if source_marker not in members or packaged_marker not in members:
        raise CandidateError(f"{package.name} has no packaged runtime provenance markers")
    marker_revision = _archive_member_bytes(package.archive, source_marker).decode("utf-8").strip()
    try:
        marker = json.loads(_archive_member_bytes(package.archive, packaged_marker).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateError(f"{package.name} has an invalid packaged runtime marker") from exc
    if not REVISION.fullmatch(marker_revision):
        raise CandidateError(f"{package.name} has an invalid source revision marker")
    marker_source_revision = marker.get("source_revision") if isinstance(marker, dict) else None
    marker_source_sha256 = marker.get("source_sha256") if isinstance(marker, dict) else None
    if marker_source_revision != marker_revision or not isinstance(marker_source_sha256, str) or not SHA256.fullmatch(marker_source_sha256):
        raise CandidateError(f"{package.name} has inconsistent packaged runtime provenance")
    expected_marker = {
        "schema_version": 1,
        "layout": "packaged",
        "runtime_mode": "packaged",
        "channel": channel,
        "version": package.version,
        "source_revision": marker_revision,
        "source_sha256": marker_source_sha256,
    }
    if marker != expected_marker:
        raise CandidateError(f"{package.name} packaged runtime marker has unexpected fields")
    if source_revision is not None and marker_revision != source_revision:
        raise CandidateError(f"{package.name} source marker differs from its candidate pair")
    if source_sha256 is not None and marker_source_sha256 != source_sha256:
        raise CandidateError(f"{package.name} source archive marker differs from its candidate pair")


def _source_inventory(root: Path) -> dict[str, tuple[str, str]]:
    inventory: dict[str, tuple[str, str]] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if relative == ".omarchy-pi-source-commit":
            continue
        info = path.lstat()
        if info.st_mode & 0o170000 == 0o120000:
            inventory[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            inventory[relative] = ("directory", "")
        elif path.is_file():
            inventory[relative] = ("file", _digest(path))
        else:
            raise CandidateError(f"source tree contains an unsupported file: {path}")
    return inventory


def _extract_source_archive(archive: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    try:
        stream = tarfile.open(archive, "r:")
    except (OSError, tarfile.TarError) as exc:
        raise CandidateError(f"candidate source archive is not a readable tar archive: {archive}") from exc
    with stream:
        for member in stream.getmembers():
            relative = Path(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise CandidateError(f"candidate source archive contains an unsafe path: {member.name}")
            target = destination / relative
            if member.isdir():
                target.mkdir(mode=member.mode & 0o7777, parents=True, exist_ok=True)
            elif member.issym():
                link_target = Path(os.path.normpath(os.fspath(target.parent / member.linkname)))
                try:
                    link_target.relative_to(destination)
                except ValueError as exc:
                    raise CandidateError(f"candidate source archive contains an escaping symlink: {member.name}") from exc
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.symlink_to(member.linkname)
            elif member.isfile():
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                source = stream.extractfile(member)
                if source is None:
                    raise CandidateError(f"candidate source archive member is unreadable: {member.name}")
                with source, target.open("wb") as output:
                    output.write(source.read())
                target.chmod(member.mode & 0o7777)
            else:
                raise CandidateError(f"candidate source archive contains an unsupported member: {member.name}")


def _validate_source_archive(archive: Path, source_tree: Path, source_sha256: str) -> None:
    if archive.is_symlink() or not archive.is_file():
        raise CandidateError(f"candidate source archive is not a regular file: {archive}")
    if _digest(archive) != source_sha256:
        raise CandidateError("candidate source archive digest differs from its pair manifest")
    with tempfile.TemporaryDirectory(prefix="omarchy-pi-source-verify-") as temporary:
        extracted = Path(temporary) / "source"
        _extract_source_archive(archive, extracted)
        if _source_inventory(extracted) != _source_inventory(source_tree):
            raise CandidateError("candidate source tree differs from its deterministic Git archive")


def _source_tree(root: Path, source: object, revision: object) -> tuple[str, str, Path, Path]:
    if not isinstance(source, dict) or not isinstance(revision, str) or not REVISION.fullmatch(revision):
        raise CandidateError("candidate source must record a full 40-character revision")
    source_sha256 = source.get("archive_sha256", source.get("sha256"))
    if not isinstance(source_sha256, str) or not SHA256.fullmatch(source_sha256):
        raise CandidateError("candidate source must record its archive SHA-256")
    tree = _safe_child(root, source.get("tree"), "candidate source tree")
    archive = _safe_child(root, source.get("archive"), "candidate source archive")
    if tree.is_symlink() or not tree.is_dir():
        raise CandidateError(f"candidate source tree is not a directory: {tree}")
    tree_root = tree.resolve(strict=True)
    for path in tree.rglob("*"):
        if not (path.is_symlink() or path.is_dir() or path.is_file()):
            raise CandidateError("candidate source tree contains an unsupported file")
        if path.is_symlink():
            try:
                path.resolve(strict=False).relative_to(tree_root)
            except ValueError as exc:
                raise CandidateError("candidate source tree contains an escaping symlink") from exc
    if not (tree / "migrations").is_dir():
        raise CandidateError("candidate source tree has no migrations directory")
    policy = tree / "install/arm64/migrations.allowlist"
    if policy.is_symlink() or not policy.is_file():
        raise CandidateError("candidate source tree has no ARM migration allowlist")
    marker = tree / ".omarchy-pi-source-commit"
    if marker.is_symlink() or not marker.is_file() or marker.read_text(encoding="utf-8").strip() != revision:
        raise CandidateError("candidate source tree revision marker differs from its manifest")
    if (tree / ".git").is_dir() or (tree / ".git").is_file():
        try:
            result = subprocess.run(
                ["/usr/bin/git", "-C", str(tree), "rev-parse", "--verify", "HEAD^{commit}"],
                check=True, capture_output=True, text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise CandidateError("candidate source revision could not be verified") from exc
        if result.stdout.strip().lower() != revision:
            raise CandidateError("candidate source tree revision differs from its manifest")
    _validate_source_archive(archive, tree, source_sha256)
    return revision, source_sha256, tree, archive


def _pair_document(root: Path) -> dict[str, Any] | None:
    """Adapt the runtime builder's ``pair.json`` into the updater schema."""

    path = root / "pair.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        pair = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CandidateError(f"package pair manifest is not valid JSON: {path}") from exc
    if not isinstance(pair, dict) or pair.get("schema_version") != 1:
        raise CandidateError("unsupported package pair manifest schema")
    source = pair.get("source")
    if not isinstance(source, dict):
        raise CandidateError("package pair has no source provenance")
    channel = pair.get("channel", "stable")
    if channel not in PACKAGE_PAIRS:
        raise CandidateError(f"unsupported Omarchy package channel: {channel}")
    package_map = pair.get("packages")
    if not isinstance(package_map, dict):
        raise CandidateError("package pair has no package records")
    packages = []
    for name in PACKAGE_PAIRS[channel]:
        record = package_map.get(name)
        if not isinstance(record, dict):
            raise CandidateError(f"package pair has no {name} record")
        signature_name = record.get("signature")
        package = {
            "name": name,
            "version": pair.get("version"),
            "architecture": pair.get("architecture"),
            "filename": record.get("filename"),
            "sha256": record.get("sha256"),
            "signature": "required" if isinstance(signature_name, str) and signature_name else "optional",
        }
        if isinstance(signature_name, str) and signature_name:
            package["signature_file"] = signature_name
        packages.append(package)
    document: dict[str, Any] = {
        "schema_version": 1,
        "architecture": pair.get("architecture"),
        "channel": channel,
        "source_revision": source.get("revision"),
        "source": {
            "tree": "source",
            "archive": source.get("archive", "source.tar"),
            "archive_sha256": source.get("archive_sha256"),
        },
        "packages": packages,
    }
    previous_path = root / "previous.json"
    if previous_path.is_file() and not previous_path.is_symlink():
        try:
            document["previous_packages"] = json.loads(previous_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CandidateError(f"previous package manifest is not valid JSON: {previous_path}") from exc
    return document


def _builder_manifest_document(root: Path) -> dict[str, Any] | None:
    """Adapt the package builder's JSON archive manifest when present."""

    path = root / "manifest.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CandidateError(f"package manifest is not valid JSON: {path}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise CandidateError("unsupported package archive manifest schema")
    records = manifest.get("packages", manifest.get("archives"))
    if isinstance(records, dict):
        records = [{"name": name, **(record if isinstance(record, dict) else {})} for name, record in records.items()]
    if not isinstance(records, list):
        raise CandidateError("package archive manifest has no package records")
    source = manifest.get("source")
    if isinstance(source, dict):
        source = {
            "revision": source.get("revision", manifest.get("source_revision")),
            "archive": source.get("archive", "source.tar"),
            "archive_sha256": source.get("archive_sha256", manifest.get("source_sha256")),
        }
    else:
        source = {
            "revision": manifest.get("source_revision"),
            "archive": "source.tar",
            "archive_sha256": manifest.get("source_sha256"),
        }
    packages = []
    source_revision = source.get("revision")
    source_sha256 = source.get("archive_sha256")
    for record in records:
        if not isinstance(record, dict):
            raise CandidateError("package archive manifest has an invalid package record")
        if record.get("source_revision", source_revision) != source_revision:
            raise CandidateError("package archive source revisions do not match the pair")
        if record.get("source_sha256", source_sha256) != source_sha256:
            raise CandidateError("package archive source SHA-256 values do not match the pair")
        signature_name = record.get("signature")
        signature_policy = "required" if record.get("package_signature") in {"provided", "required"} or (isinstance(signature_name, str) and signature_name) else "optional"
        package = {
            "name": record.get("package", record.get("name")),
            "version": record.get("version", manifest.get("version")),
            "architecture": record.get("architecture", manifest.get("architecture")),
            "filename": record.get("filename"),
            "sha256": record.get("sha256", record.get("package_sha256")),
            "signature": signature_policy,
        }
        if isinstance(signature_name, str) and signature_name:
            package["signature_file"] = signature_name
        packages.append(package)
    document: dict[str, Any] = {
        "schema_version": 1,
        "architecture": manifest.get("architecture"),
        "channel": manifest.get("channel", "stable"),
        "source_revision": source_revision,
        "source": {"tree": "source", "archive": source["archive"], "archive_sha256": source_sha256},
        "packages": packages,
    }
    previous_path = root / "previous.json"
    if previous_path.is_file() and not previous_path.is_symlink():
        try:
            document["previous_packages"] = json.loads(previous_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CandidateError(f"previous package manifest is not valid JSON: {previous_path}") from exc
    return document


def load_candidate(root: str | Path = DEFAULT_CANDIDATE_ROOT, *, require_previous: bool = True) -> Candidate:
    candidate_root = Path(root)
    if candidate_root.is_symlink() or not candidate_root.is_dir():
        raise CandidateError(f"package candidate directory is unavailable: {candidate_root}")
    manifest = candidate_root / "candidate.json"
    pair_manifest = _pair_document(candidate_root)
    builder_manifest = None if manifest.is_file() else _builder_manifest_document(candidate_root)
    if not manifest.is_file() and pair_manifest is not None:
        # pair.json is the native builder output.  It is not rewritten: the
        # returned object retains its exact path for launch/worker pinning.
        manifest = candidate_root / "pair.json"
        document = pair_manifest
    elif not manifest.is_file() and builder_manifest is not None:
        manifest = candidate_root / "manifest.json"
        document = builder_manifest
    else:
        document = None
    if manifest.is_symlink() or not manifest.is_file():
        raise CandidateError(f"package candidate manifest is unavailable: {manifest}")
    if document is None:
        try:
            document = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CandidateError(f"package candidate manifest is not valid JSON: {manifest}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise CandidateError("unsupported package candidate manifest schema")
    if document.get("architecture") != "aarch64":
        raise CandidateError("package candidate is not for aarch64")
    channel = document.get("channel", "stable")
    if channel not in PACKAGE_PAIRS:
        raise CandidateError(f"unsupported Omarchy candidate channel: {channel}")
    source = document.get("source")
    revision, source_sha256, source_tree, source_archive = _source_tree(candidate_root, source, document.get("source_revision"))
    records = document.get("packages")
    if not isinstance(records, list):
        raise CandidateError("candidate package list is missing")
    packages = tuple(_record_package(candidate_root, record, f"package {index + 1}") for index, record in enumerate(records))
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise CandidateError(f"package {index + 1} is not an object")
        if record.get("source_revision", revision) != revision:
            raise CandidateError("candidate package source revisions do not match the pair")
        if record.get("source_sha256", source_sha256) != source_sha256:
            raise CandidateError("candidate package source SHA-256 values do not match the pair")
    expected = PACKAGE_PAIRS[channel]
    if len(packages) != len(expected) or {package.name for package in packages} != set(expected):
        raise CandidateError("candidate must contain the matching Omarchy runtime/settings pair")
    if {package.version for package in packages} != {packages[0].version}:
        raise CandidateError("candidate Omarchy packages do not share a compatible pair version")
    for package in packages:
        _validate_native_package(
            package, channel, source_revision=revision, source_sha256=source_sha256,
        )
    rollback = document.get("previous_packages")
    if rollback is None and not require_previous:
        previous = ()
    else:
        if not isinstance(rollback, list) or not rollback:
            raise CandidateError("candidate must retain previous package archives for rollback")
        previous = tuple(_record_package(candidate_root, record, f"previous package {index + 1}") for index, record in enumerate(rollback))
        previous_names = {package.name for package in previous}
        if not set(expected).issubset(previous_names):
            raise CandidateError("candidate rollback set must include both previous Omarchy packages")
        for package in previous:
            _validate_native_package(package, channel)
    return Candidate(candidate_root, manifest, channel, revision, source_sha256, source_tree, source_archive, packages, previous)


def _rollback_document(root: Path) -> dict[str, Any]:
    for name in ("manifest.json", "pair.json", "rollback.json"):
        path = root / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CandidateError(f"rollback manifest is not valid JSON: {path}") from exc
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise CandidateError(f"rollback manifest has an unsupported schema: {path}")
        return document
    raise CandidateError(f"no retained Omarchy rollback manifest exists under {root}")


def load_installed_rollback(
    expected_names: Iterable[str],
    roots: Iterable[str | Path] = ROLLBACK_ROOTS,
) -> tuple[CandidatePackage, ...]:
    """Load and hash the immutable previous pair retained outside user home."""

    expected = set(expected_names)
    errors: list[str] = []
    for root_value in roots:
        root = Path(root_value)
        if root.is_symlink() or not root.is_dir():
            continue
        try:
            document = _rollback_document(root)
            records = document.get("packages", document.get("archives"))
            if isinstance(records, dict):
                records = [{"name": name, **(record if isinstance(record, dict) else {})} for name, record in records.items()]
            if not isinstance(records, list):
                raise CandidateError("rollback manifest has no package records")
            packages: list[CandidatePackage] = []
            for index, record in enumerate(records):
                if not isinstance(record, dict):
                    raise CandidateError(f"rollback package {index + 1} is not an object")
                signature_value = record.get("signature")
                if signature_value in {"required", "optional"}:
                    signature_policy = signature_value
                elif record.get("package_signature") == "provided" or (isinstance(signature_value, str) and signature_value):
                    signature_policy = "required"
                else:
                    signature_policy = "optional"
                normalized = {
                    "name": record.get("name", record.get("package")),
                    "version": record.get("version", document.get("version")),
                    "architecture": record.get("architecture", document.get("architecture", "aarch64")),
                    "filename": record.get("filename"),
                    "sha256": record.get("sha256", record.get("package_sha256")),
                    "signature": signature_policy,
                }
                signature_file = record.get("signature_file")
                if signature_file is None and signature_policy == "required" and isinstance(signature_value, str) and signature_value not in {"required", "optional"}:
                    signature_file = signature_value
                if isinstance(signature_file, str) and signature_file:
                    normalized["signature_file"] = signature_file
                packages.append(_record_package(root, normalized, f"rollback package {index + 1}"))
            if {package.name for package in packages} != expected:
                raise CandidateError("rollback manifest does not contain the installed Omarchy pair")
            if len({package.version for package in packages}) != 1:
                raise CandidateError("rollback pair versions do not match")
            channel = next((name for name, pair in PACKAGE_PAIRS.items() if set(pair) == expected), None)
            if channel is None:
                raise CandidateError("rollback manifest contains an unsupported Omarchy pair")
            for package in packages:
                _validate_native_package(package, channel, retained=True)
            return tuple(packages)
        except CandidateError as error:
            errors.append(str(error))
    detail = errors[-1] if errors else "no rollback directory is available"
    raise CandidateError(f"retained Omarchy package rollback is unavailable: {detail}")


def package_provenance(
    runtime: str | Path = RUNTIME_ROOT,
    *,
    pacman: str = "/usr/bin/pacman",
    runner: Callable[..., Any] = subprocess.run,
) -> str | None:
    """Return the installed stable/dev channel proven by pacman ownership.

    Checking package ownership of files below ``/usr/share/omarchy`` keeps a
    stale ``OMARCHY_PATH`` or a source checkout from selecting package mode.
    """

    runtime = Path(runtime)
    if runtime != RUNTIME_ROOT or not runtime.is_dir() or not (runtime / "version").is_file():
        return None
    marker = runtime / PACKAGED_MARKER
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    mode = document.get("runtime_mode", document.get("mode", document.get("layout")))
    if mode != "packaged":
        return None
    marker_channel = document.get("channel")
    if marker_channel is not None and marker_channel not in PACKAGE_PAIRS:
        return None
    marker_revision = document.get("source_revision")
    if marker_revision is not None and (not isinstance(marker_revision, str) or not REVISION.fullmatch(marker_revision)):
        return None
    try:
        owners: list[str] = []
        for path in (runtime / "version", runtime / "config"):
            result = runner([pacman, "-Qo", "--quiet", "--", str(path)], check=False, capture_output=True, text=True)
            if result.returncode != 0 or not result.stdout.strip():
                return None
            owner = _pacman_owner(result.stdout)
            if owner is None:
                return None
            owners.append(owner)
    except OSError:
        return None
    channel = None
    if owners[0] == "omarchy" and owners[1] == "omarchy-settings":
        channel = "stable"
    elif owners[0] == "omarchy-dev" and owners[1] == "omarchy-settings-dev":
        channel = "dev"
    if channel is not None and (marker_channel is None or marker_channel == channel):
        return channel
    return None


def installed_source_revision(runtime: str | Path | None = None) -> str | None:
    """Read the source revision recorded by the installed package marker."""

    if runtime is None:
        runtime_text = "/usr/share/omarchy"
        if os.environ.get("OMARCHY_PI_TESTING") == "1" and os.environ.get("OMARCHY_PI_TEST_RUNTIME_ROOT"):
            runtime_text = os.environ["OMARCHY_PI_TEST_RUNTIME_ROOT"]
        runtime = runtime_text
    marker = Path(runtime) / PACKAGED_MARKER
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    revision = document.get("source_revision") if isinstance(document, dict) else None
    return revision if isinstance(revision, str) and REVISION.fullmatch(revision) else None


def local_transaction_command(
    candidate: Candidate,
    *,
    pacman: str = "/usr/bin/pacman",
    force: bool = False,
) -> list[str]:
    """Build the reviewed local-archive transaction for either direction.

    A changed source revision must reinstall even if a rebased downstream
    branch reused the same package version. ``force`` deliberately remains a
    local transaction choice; repository package resolution is unaffected.
    """

    needed = [] if force else ["--needed"]
    return [pacman, "-U", *needed, "--noconfirm", *(str(package.archive) for package in candidate.packages)]


def needs_optional_local_signature(candidate: Candidate) -> bool:
    # Only the pair passed to the current ``pacman -U`` needs this policy.
    # Retained rollback archives are validated offline and are not part of the
    # transaction, so an unsigned historical pair must not relax a signed one.
    return any(package.signature == "optional" for package in candidate.packages)


def pair_is_current(
    candidate: Candidate,
    installed_versions: dict[str, str],
    installed_source_revision: str | None,
    installed_packages: Iterable[CandidatePackage] = (),
) -> bool:
    """Require versions, source marker, and retained archive hashes to match."""

    if not (
        installed_source_revision == candidate.source_revision
        and all(installed_versions.get(package.name) == package.version for package in candidate.packages)
    ):
        return False
    retained = {package.name: package for package in installed_packages}
    return all(
        package.name in retained and retained[package.name].sha256 == package.sha256
        for package in candidate.packages
    ) if retained else True


def installed_package_versions(
    candidate: Candidate,
    *,
    pacman: str = "/usr/bin/pacman",
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, str]:
    """Read the installed pair versions for status and no-op detection."""

    try:
        result = runner([pacman, "-Q", *candidate.package_names], check=False, capture_output=True, text=True)
    except OSError:
        return {}
    if result.returncode != 0:
        return {}
    versions: dict[str, str] = {}
    expected = set(candidate.package_names)
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] in expected:
            versions[fields[0]] = fields[1]
    return versions


def candidate_actions(candidate: Candidate, installed_versions: dict[str, str], *, vercmp: str = "/usr/bin/vercmp") -> dict[str, str]:
    """Classify each pair member without changing the selected ``pacman -U`` command."""

    actions: dict[str, str] = {}
    for package in candidate.packages:
        installed = installed_versions.get(package.name)
        if installed is None:
            actions[package.name] = "install"
            continue
        try:
            result = subprocess.run([vercmp, installed, package.version], check=True, capture_output=True, text=True)
            comparison = int(result.stdout.strip())
        except (OSError, ValueError, subprocess.CalledProcessError):
            actions[package.name] = "unknown"
            continue
        actions[package.name] = "upgrade" if comparison < 0 else "downgrade" if comparison > 0 else "current"
    return actions


def candidate_status(candidate: Candidate) -> str:
    """Stable one-line status used by ``omarchy-update-available``."""

    return f"omarchy-{candidate.channel}-candidate {candidate.source_revision} available"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check",))
    parser.add_argument("--root", type=Path, default=DEFAULT_CANDIDATE_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        # The user-side prepare step publishes a new pair before the root
        # worker binds the retained installed pair for rollback.  Status only
        # needs to validate the candidate itself; the worker still fails
        # closed if the installed rollback archive is absent.
        candidate = load_candidate(args.root, require_previous=False)
    except CandidateError as error:
        print(f"omarchy package candidate unavailable: {error}")
        return 2
    try:
        retained = load_installed_rollback(candidate.package_names)
    except CandidateError:
        retained = ()
    if pair_is_current(candidate, installed_package_versions(candidate), installed_source_revision(), retained):
        print(f"omarchy-{candidate.channel}-candidate {candidate.source_revision} current")
        return 1
    print(candidate_status(candidate))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
