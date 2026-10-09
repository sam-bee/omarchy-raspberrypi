#!/usr/bin/env python3
"""Package a verified Pi installer image for a GitHub release.

This helper is deliberately file-only.  It consumes an already-built raw
image and its provenance, compresses the image with the release settings,
splits the stream below GitHub's asset limit, and writes the release metadata
used by the existing Pi 5 release workflow.  It never opens a device, mounts
anything, contacts a Pi, or publishes an asset.

The final invocation must provide an explicit acceptance record bound to the
selected raw image.  It may record an exact-image native boot or an inherited
native baseline plus final-image runtime and file validation; the generated
manifest states which scope was actually tested.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Iterable, Mapping


PART_BYTES = 1_900_000_000
COMPRESSION_LEVEL = 1
COMPRESSION_THREADS = 2
SHA256_RE = set("0123456789abcdef")


class ReleaseError(RuntimeError):
    """Raised when the release inputs do not form one consistent image."""


@dataclass(frozen=True)
class Digest:
    bytes: int
    sha256: str


def _regular_file(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReleaseError(f"{label} is unavailable: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReleaseError(f"{label} must be a regular file: {path}")
    return path


def _sha256(path: Path) -> Digest:
    _regular_file(path, "input")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return Digest(size, digest.hexdigest())


def _read_json(path: Path, label: str) -> dict[str, Any]:
    _regular_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"{label} is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ReleaseError(f"{label} must contain a JSON object: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _first_string(objects: Iterable[Mapping[str, Any]], *keys: str) -> str:
    for obj in objects:
        for key in keys:
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
    raise ReleaseError(f"missing provenance field: {', '.join(keys)}")


def _valid_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in SHA256_RE for ch in value):
        raise ReleaseError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _valid_revision(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or any(ch not in SHA256_RE for ch in value):
        raise ReleaseError(f"{label} must be a lowercase 40-character Git revision")
    return value


def _run(command: list[str], *, stdout: int | Any = subprocess.PIPE) -> bytes:
    try:
        result = subprocess.run(command, check=True, stdout=stdout, stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError) as exc:
        rendered = " ".join(command)
        detail = exc.stderr.decode(errors="replace").strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise ReleaseError(f"command failed: {rendered}: {detail}") from exc
    return result.stdout if isinstance(result.stdout, bytes) else b""


def _load_candidate(candidate: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    _regular_file(candidate / "output-image.json", "candidate output manifest")
    output = _read_json(candidate / "output-image.json", "candidate output manifest")
    provenance = _read_json(candidate / "provenance.json", "candidate provenance")
    receipt = _read_json(candidate / "build-receipt.json", "candidate build receipt")
    return output, provenance, receipt


def _candidate_image(candidate: Path, output: Mapping[str, Any]) -> Path:
    raw = output.get("raw")
    if not isinstance(raw, Mapping):
        raise ReleaseError("candidate output manifest has no raw image record")
    name = raw.get("name")
    if not isinstance(name, str) or Path(name).name != name:
        raise ReleaseError("candidate raw image name is invalid")
    image = candidate / name
    expected = _valid_sha(raw.get("sha256"), "candidate raw image hash")
    expected_bytes = raw.get("bytes")
    digest = _sha256(image)
    if digest.sha256 != expected or digest.bytes != expected_bytes:
        raise ReleaseError("candidate raw image does not match output-image.json")
    return image


def _extract_desktop_manifest(archive: Path) -> tuple[dict[str, Any], bytes]:
    _regular_file(archive, "desktop payload archive")
    listing = _run(["tar", "--use-compress-program=zstd", "--list", "--file", os.fspath(archive)])
    members = [line for line in listing.decode(errors="replace").splitlines() if Path(line).name == "desktop-manifest.json"]
    if len(members) != 1:
        raise ReleaseError("desktop archive must contain exactly one desktop-manifest.json")
    data = _run(["tar", "--use-compress-program=zstd", "--extract", "--to-stdout", "--file", os.fspath(archive), members[0]])
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("desktop-manifest.json is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise ReleaseError("desktop-manifest.json must contain a JSON object")
    return document, data


def _package_rows(value: Any, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ReleaseError(f"{label} has no package records")
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise ReleaseError(f"{label} record {index + 1} is not an object")
        name = row.get("name") or row.get("package")
        version = row.get("version")
        repository = row.get("repository", "local")
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise ReleaseError(f"{label} record {index + 1} lacks name/version")
        if not isinstance(repository, str) or not repository:
            raise ReleaseError(f"{label} record {index + 1} lacks repository")
        rows.append({"name": name, "repository": repository, "version": version})
    return sorted(rows, key=lambda row: (row["name"], row["version"], row["repository"]))


def _installer_rows(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    transaction = manifest.get("transaction")
    if not isinstance(transaction, Mapping):
        raise ReleaseError("installer package manifest has no transaction")
    rows = transaction.get("resolved_packages")
    if not isinstance(rows, list):
        rows = manifest.get("packages")
    return _package_rows(rows, label="installer package manifest")


def _desktop_rows(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    transaction = manifest.get("transaction")
    if not isinstance(transaction, Mapping):
        raise ReleaseError("desktop manifest has no transaction")
    return _package_rows(transaction.get("installed_packages"), label="desktop manifest")


def _native_acceptance(
    path: Path,
    *,
    image_digest: Digest,
    installer_revision: str,
    installer_runtime_sha: str,
) -> tuple[dict[str, Any], str, dict[str, bool | None]]:
    document = _read_json(path, "native acceptance record")
    if document.get("status") != "passed":
        raise ReleaseError("native acceptance status must be passed")
    raw_sha = _valid_sha(document.get("raw_sha256"), "native acceptance raw_sha256")
    if raw_sha != image_digest.sha256:
        raise ReleaseError("native acceptance raw_sha256 does not match the selected image")
    pinned_revision = document.get("installer_source_revision")
    if pinned_revision is not None and pinned_revision != installer_revision:
        raise ReleaseError("native acceptance installer revision does not match the selected image")
    pinned_runtime_sha = document.get("installer_runtime_sha256")
    if pinned_runtime_sha is not None and pinned_runtime_sha != installer_runtime_sha:
        raise ReleaseError("native acceptance runtime digest does not match the selected image")
    scope_value = document.get("scope") or document.get("acceptance_scope")
    if isinstance(scope_value, str) and scope_value.strip():
        scope_values = [scope_value.strip()]
    elif isinstance(scope_value, list) and scope_value and all(isinstance(value, str) and value.strip() for value in scope_value):
        scope_values = [value.strip() for value in scope_value]
    else:
        checks = document.get("checks")
        if isinstance(checks, Mapping):
            scope_values = sorted(str(key) for key, value in checks.items() if value is True)
        elif isinstance(checks, list):
            scope_values = [str(value).strip() for value in checks if isinstance(value, str) and value.strip()]
        else:
            scope_values = []
    if not scope_values:
        raise ReleaseError("native acceptance record must describe its tested scope")

    validation = document.get("validation")
    if validation is not None and not isinstance(validation, Mapping):
        raise ReleaseError("native acceptance validation must be an object")

    def flag(name: str) -> bool | None:
        values = [
            obj[name]
            for obj in (document, validation or {})
            if isinstance(obj, Mapping) and name in obj
        ]
        if any(type(value) is not bool for value in values):
            raise ReleaseError(f"native acceptance {name} must be boolean")
        if len(set(values)) > 1:
            raise ReleaseError(f"native acceptance {name} is contradictory")
        return values[0] if values else None

    def normalized(value: str) -> str:
        return value.strip().lower().replace("_", "-").replace(" ", "-")

    scope_tokens = {normalized(value) for value in scope_values}

    def scoped_flag(name: str, *aliases: str) -> bool | None:
        value = flag(name)
        implied = any(alias in scope_tokens for alias in aliases)
        if value is False and implied:
            raise ReleaseError(f"native acceptance {name} conflicts with its scope")
        return value if value is not None else (True if implied else None)

    native = flag("native_boot_tested")
    if native is None:
        raise ReleaseError("native acceptance must explicitly record native_boot_tested")
    if native is False and scope_tokens & {"exact-image-native-boot", "exact-native-boot", "exact-image-boot"}:
        raise ReleaseError("native acceptance scope claims an exact-image boot while native_boot_tested is false")
    runtime_verified = scoped_flag("native_runtime_verified", "native-runtime-verified")
    file_verified = scoped_flag("file_image_verified", "file-image-verified", "final-image-file-verified")
    inherited = scoped_flag("inherited_baseline", "inherited-baseline", "baseline-inherited")
    if native is False and not (runtime_verified is True and file_verified is True and inherited is True):
        raise ReleaseError(
            "native acceptance without exact-image boot requires inherited baseline, runtime, and file validation"
        )
    return document, ", ".join(scope_values), {
        "native_boot_tested": native,
        "native_runtime_verified": runtime_verified,
        "file_image_verified": file_verified,
        "inherited_baseline": inherited,
    }


def _runtime_rows(value: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise ReleaseError(f"runtime package record {index + 1} is not an object")
        compact = {
            key: row[key]
            for key in (
                "name",
                "package",
                "version",
                "architecture",
                "filename",
                "package_sha256",
                "sha256",
                "signature",
                "package_signature",
                "source_revision",
                "source_sha256",
            )
            if key in row
        }
        if not isinstance(compact.get("name") or compact.get("package"), str) or not isinstance(compact.get("version"), str):
            raise ReleaseError(f"runtime package record {index + 1} lacks name/version")
        compact["name"] = compact.get("name") or compact["package"]
        compact.pop("package", None)
        if "package_sha256" not in compact and isinstance(compact.get("sha256"), str):
            compact["package_sha256"] = compact["sha256"]
        compact.pop("sha256", None)
        rows.append(compact)
    return sorted(rows, key=lambda row: (row["name"], row["version"]))


def _compress_and_split(image: Path, destination: Path, stem: str) -> tuple[Digest, list[Digest]]:
    zstd = shutil.which("zstd")
    if zstd is None:
        raise ReleaseError("zstd is required to package release assets")
    with tempfile.TemporaryDirectory(prefix="omarchy-release-") as temporary:
        stream = Path(temporary) / f"{stem}.img.zst"
        command = [zstd, "--quiet", f"-{COMPRESSION_LEVEL}", f"-T{COMPRESSION_THREADS}", "--stdout", os.fspath(image)]
        try:
            with stream.open("wb") as output:
                subprocess.run(command, check=True, stdout=output, stderr=subprocess.PIPE)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ReleaseError("zstd compression failed") from exc
        stream_digest = _sha256(stream)
        parts: list[Digest] = []
        with stream.open("rb") as source:
            index = 0
            while True:
                data = source.read(PART_BYTES)
                if not data:
                    break
                part = destination / f"{stem}.img.zst.part-{index:02d}"
                part.write_bytes(data)
                parts.append(_sha256(part))
                index += 1
        if not parts:
            raise ReleaseError("compression produced an empty stream")
    return stream_digest, parts


def _sum_lines(paths: Iterable[Path], root: Path) -> str:
    lines = []
    for path in sorted(paths, key=lambda item: item.name):
        digest = _sha256(path)
        lines.append(f"{digest.sha256}  {path.relative_to(root)}")
    return "\n".join(lines) + "\n"


def _release_text(
    release: str,
    stem: str,
    image_digest: Digest,
    stream_digest: Digest,
    parts: list[Digest],
    installer_revision: str,
    installer_runtime_sha: str,
    desktop_revision: str,
    desktop_archive_digest: Digest,
    desktop_rows: list[dict[str, Any]],
    runtime_packages: list[Mapping[str, Any]],
    acceptance: Mapping[str, Any],
    acceptance_scope: str,
    acceptance_flags: Mapping[str, bool | None],
) -> str:
    part_lines = "\n".join(
        f"- `{stem}.img.zst.part-{index:02d}` — {part.bytes:,} bytes; SHA-256 `{part.sha256}`"
        for index, part in enumerate(parts)
    )
    cat_lines = (" " + "\\" + "\n    ").join(
        f"{stem}.img.zst.part-{index:02d}" for index in range(len(parts))
    )
    runtime_lines = "\n".join(
        f"- `{row.get('name')}` `{row.get('version')}` ({row.get('architecture', 'aarch64')}); archive SHA-256 `{row.get('package_sha256', row.get('sha256', ''))}`"
        for row in runtime_packages
    )
    return f"""# Omarchy Pi 5 {release}

This production release packages the Raspberry Pi 5 installer image whose acceptance scope is recorded below. The record explicitly states whether this exact raw image was booted natively; the scope is tied to this image's raw SHA-256.

## Image and provenance

- Raw image: `{stem}.img`; {image_digest.bytes:,} bytes; SHA-256 `{image_digest.sha256}`
- Reconstructed zstd stream: `{stem}.img.zst`; {stream_digest.bytes:,} bytes; SHA-256 `{stream_digest.sha256}`
- Installer runtime source: `{installer_revision}`
- Installer runtime digest: `{installer_runtime_sha}`
- Desktop payload source: `{desktop_revision}`
- Desktop payload: {len(desktop_rows)} installed package records; {desktop_archive_digest.bytes:,} bytes; SHA-256 `{desktop_archive_digest.sha256}`
- Acceptance record: `{acceptance.get('status', 'passed')}`; exact-image native boot tested: `{str(acceptance_flags['native_boot_tested']).lower()}`; scope: {acceptance_scope}

The compressed stream is split below GitHub's 2 GiB asset limit:

{part_lines}

The raw image and unsplit stream are recorded for provenance; publish the numbered parts and metadata files as release assets.

## Reconstruct and verify the image

Download every numbered part and `SHA256SUMS` into one directory, then run:

```sh
sha256sum --ignore-missing --check SHA256SUMS
cat {cat_lines} > {stem}.img.zst
sha256sum {stem}.img.zst
zstd --test {stem}.img.zst
zstd --decompress --keep {stem}.img.zst
sha256sum {stem}.img
```

The stream and raw hashes must match the values above. Write the resulting `.img` to the whole approved installer USB only after the separate hardware and media checks are complete.

## Packaged Omarchy runtime

The desktop uses the package-owned `/usr/share/omarchy` runtime and records the exact Omarchy package pair in `package-versions.json`:

{runtime_lines}

The package pair and Pi-specific installer source are part of the image provenance. Use the packaged update path documented by the matching source revision; do not substitute a user-owned source checkout when comparing this release.

## Build and acceptance

The release manifest and package inventories record the image, package, desktop, source and acceptance pins. Acceptance covers the exact raw image hash above and the updated runtime scope listed above; `native_boot_tested` is true only when the record proves that exact image booted. The earlier full encrypted installation remains baseline evidence for the installer workflow; it is not claimed as a second fresh installation of this image.

## License

See `LICENSES.md`. Omarchy source and project-specific changes are released under MIT. Bundled Arch Linux ARM packages, Raspberry Pi firmware and third-party components retain their respective licenses; their license texts remain in `/usr/share/licenses/<package>/` in the installed image.
"""


def build(args: argparse.Namespace) -> Path:
    release = args.release
    if not release or any(ch.isspace() for ch in release):
        raise ReleaseError("--release must be a non-empty identifier without whitespace")
    candidate = args.candidate_dir.resolve()
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        if not output_dir.is_dir() or any(output_dir.iterdir()):
            raise ReleaseError(f"refusing non-empty release directory: {output_dir}")
    else:
        output_dir.mkdir(parents=True)
    output, provenance, receipt = _load_candidate(candidate)
    image = _candidate_image(candidate, output)
    stem = args.image_stem or image.name.removesuffix(".img")
    if Path(stem).name != stem or not stem:
        raise ReleaseError("--image-stem must be a simple filename stem")

    desktop_archive = args.desktop_archive or candidate / "installer-root/usr/local/share/omarchy-pi/desktop/desktop.tar.zst"
    desktop_manifest, desktop_manifest_bytes = _extract_desktop_manifest(desktop_archive)
    desktop_archive_digest = _sha256(desktop_archive)
    desktop_rows = _desktop_rows(desktop_manifest)
    runtime = desktop_manifest.get("runtime")
    runtime_packages_raw = runtime.get("packages") if isinstance(runtime, Mapping) else None
    if not isinstance(runtime_packages_raw, list) or len(runtime_packages_raw) != 2:
        raise ReleaseError("desktop manifest must contain the two packaged Omarchy runtime records")
    runtime_packages = _runtime_rows(runtime_packages_raw)

    installer_manifest = _read_json(args.installer_package_manifest, "installer package manifest")
    installer_rows = _installer_rows(installer_manifest)
    installer_manifest_digest = _sha256(args.installer_package_manifest)
    output_raw = output.get("raw")
    installer_record = output.get("installer")
    desktop_record = output.get("desktop")
    if not isinstance(output_raw, Mapping) or not isinstance(installer_record, Mapping) or not isinstance(desktop_record, Mapping):
        raise ReleaseError("candidate output manifest lacks raw/installer/desktop records")
    installer_revision = _valid_revision(provenance.get("installer", {}).get("source_revision") if isinstance(provenance.get("installer"), Mapping) else output.get("installer", {}).get("source_revision"), "installer source revision")
    installer_runtime_sha = _valid_sha(output.get("installer", {}).get("runtime_sha256"), "installer runtime digest")
    desktop_revision = _valid_revision(output.get("desktop", {}).get("source_revision"), "desktop source revision")
    image_digest = _sha256(image)
    acceptance, acceptance_scope, acceptance_flags = _native_acceptance(
        args.native_acceptance,
        image_digest=image_digest,
        installer_revision=installer_revision,
        installer_runtime_sha=installer_runtime_sha,
    )

    stream_digest, parts = _compress_and_split(image, output_dir, stem)
    package_versions = {
        "schema_version": 1,
        "release": release,
        "installer": {
            "source_revision": installer_revision,
            "package_records": len(installer_rows),
            "package_manifest_sha256": installer_manifest_digest.sha256,
            "packages": installer_rows,
        },
        "desktop": {
            "source_revision": desktop_revision,
            "package_records": len(desktop_rows),
            "payload_sha256": desktop_archive_digest.sha256,
            "desktop_manifest_sha256": hashlib.sha256(desktop_manifest_bytes).hexdigest(),
            "packages": desktop_rows,
            "runtime_packages": runtime_packages,
        },
    }
    package_json = output_dir / "package-versions.json"
    _write_json(package_json, package_versions)
    package_txt = output_dir / "package-versions.txt"
    package_txt.write_text(
        "\n".join(
            [
                f"# Omarchy Pi 5 {release} package and source pins",
                f"installer-runtime-source={installer_revision}",
                f"installer-runtime-sha256={installer_runtime_sha}",
                f"installer-package-records={len(installer_rows)}",
                f"installer-package-manifest-sha256={installer_manifest_digest.sha256}",
                f"desktop-payload-source={desktop_revision}",
                f"desktop-payload-package-records={len(desktop_rows)}",
                f"desktop-payload-sha256={desktop_archive_digest.sha256}",
                f"desktop-manifest-sha256={hashlib.sha256(desktop_manifest_bytes).hexdigest()}",
                "full-package-inventory=package-versions.json",
                *[f"runtime-package={row.get('name')} {row.get('version')} {row.get('package_sha256', row.get('sha256', ''))}" for row in runtime_packages],
                "",
            ]
        ),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "release": release,
        "compression": {
            "format": "zstd",
            "level": COMPRESSION_LEVEL,
            "threads": COMPRESSION_THREADS,
            "stream_bytes": stream_digest.bytes,
            "stream_sha256": stream_digest.sha256,
        },
        "image": {"filename": f"{stem}.img", "bytes": image_digest.bytes, "sha256": image_digest.sha256},
        "parts": [
            {"filename": f"{stem}.img.zst.part-{index:02d}", "bytes": part.bytes, "sha256": part.sha256}
            for index, part in enumerate(parts)
        ],
        "sources": {
            "installer_runtime_revision": installer_revision,
            "installer_runtime_sha256": installer_runtime_sha,
            "desktop_payload_revision": desktop_revision,
            "desktop_payload_sha256": desktop_archive_digest.sha256,
            "desktop_payload_package_records": len(desktop_rows),
            "installer_package_manifest_sha256": installer_manifest_digest.sha256,
            "runtime_packages": runtime_packages,
        },
        "verification": {
            "native_boot_tested": acceptance_flags["native_boot_tested"],
            "native_acceptance_status": acceptance.get("status", "passed"),
            "native_acceptance_scope": acceptance_scope,
            "native_acceptance_raw_sha256": image_digest.sha256,
            "physical_devices_touched_during_packaging": False,
            "raw_and_compressed_hashes_rechecked_locally": True,
            "part_hashes_rechecked_locally": True,
            "candidate_validation": receipt.get("validation") or output.get("validation", {}),
        },
    }
    for key in ("native_runtime_verified", "file_image_verified", "inherited_baseline"):
        if acceptance_flags[key] is not None:
            manifest["verification"][key] = acceptance_flags[key]
    manifest_path = output_dir / "release-manifest.json"
    _write_json(manifest_path, manifest)
    licenses_source = args.licenses_source or Path(__file__).with_name("omarchy-pi5-0.1.0-pi5-2026.09.29") / "LICENSES.md"
    _regular_file(licenses_source, "license template")
    shutil.copyfile(licenses_source, output_dir / "LICENSES.md")
    release_path = output_dir / "RELEASE.md"
    release_path.write_text(
        _release_text(
            release,
            stem,
            image_digest,
            stream_digest,
            parts,
            installer_revision,
            installer_runtime_sha,
            desktop_revision,
            desktop_archive_digest,
            desktop_rows,
            runtime_packages,
            acceptance,
            acceptance_scope,
            acceptance_flags,
        ),
        encoding="utf-8",
    )
    sums_paths = [
        *[output_dir / f"{stem}.img.zst.part-{index:02d}" for index in range(len(parts))],
        output_dir / "LICENSES.md",
        package_json,
        package_txt,
        release_path,
        manifest_path,
    ]
    (output_dir / "SHA256SUMS").write_text(_sum_lines(sums_paths, output_dir), encoding="utf-8")
    return output_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, help="release identifier, for example 0.2.0-pi5-2026.10.09")
    parser.add_argument("--candidate-dir", type=Path, required=True, help="completed image candidate directory")
    parser.add_argument("--output-dir", type=Path, required=True, help="new empty release metadata directory")
    parser.add_argument("--installer-package-manifest", type=Path, required=True, help="native stage-arm package manifest")
    parser.add_argument("--native-acceptance", type=Path, required=True, help="acceptance record bound to the selected raw image")
    parser.add_argument("--desktop-archive", type=Path, help="desktop.tar.zst; defaults to the candidate payload")
    parser.add_argument("--image-stem", help="release image stem; defaults to the candidate raw image stem")
    parser.add_argument("--licenses-source", type=Path, help="license note template")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        output = build(build_parser().parse_args(argv))
    except ReleaseError as exc:
        print(f"package-release: {exc}", file=os.sys.stderr)
        return 2
    print(f"release metadata and split assets: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
