#!/usr/bin/python3
"""Build one Raspberry Pi installer image from already verified inputs.

The default invocation is a non-mutating plan.  ``--apply`` is required for
rootfs extraction, package installation, initramfs generation, service
staging, and image assembly.  The work directory must be new for every apply
run, so a partial build cannot be mistaken for a completed one.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
from typing import Any, Sequence


HERE = Path(__file__).resolve().parent
FINGERPRINT = re.compile(r"[0-9A-Fa-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class InstallerBuildError(RuntimeError):
    """Raised when an installer build cannot be started or completed safely."""


def _load_module(filename: str, name: str) -> Any:
    path = HERE / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise InstallerBuildError(f"cannot load build component {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _components() -> dict[str, Any]:
    # Hyphenated filenames are intentionally loaded as modules only after the
    # plan has been accepted; importing them has no package/media side effects.
    return {
        "rootfs": _load_module("prepare-signed-rootfs.py", "installer_prepare_signed_rootfs"),
        "packages": _load_module("stage-arm-packages.py", "installer_stage_arm_packages"),
        "boot": _load_module("configure-installer-boot.py", "installer_configure_boot"),
        "services": _load_module("stage-installer-services.py", "installer_stage_services"),
        "image": _load_module("assemble-image.py", "installer_assemble_image"),
        "desktop": _load_module("desktop_payload.py", "installer_desktop_payload"),
        "verify": _load_module("verify-installer-image.py", "installer_verify_image"),
    }


def _absolute(path: Path) -> Path:
    if path.is_absolute():
        return Path(os.path.normpath(os.fspath(path)))
    return Path(os.path.normpath(os.path.join(os.getcwd(), os.fspath(path))))


def _reject_symlink_components(path: Path, *, include_leaf: bool = True) -> None:
    candidate = _absolute(path)
    components = candidate.parts[1:]
    if not include_leaf:
        components = components[:-1]
    current = Path(candidate.anchor)
    for component in components:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise InstallerBuildError(f"refusing symlink path component: {current}")


def _regular_file(path: Path, *, name: str) -> Path:
    candidate = _absolute(path)
    _reject_symlink_components(candidate)
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise InstallerBuildError(f"{name} is unavailable: {candidate}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size == 0:
        raise InstallerBuildError(f"{name} must be a nonempty regular file: {candidate}")
    return candidate


def _validate_digest(value: str, *, name: str) -> str:
    if not SHA256.fullmatch(value):
        raise InstallerBuildError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _validate_fingerprint(value: str) -> str:
    normalized = value.replace(" ", "").upper()
    if not FINGERPRINT.fullmatch(normalized):
        raise InstallerBuildError("signer fingerprint must be the full 40-hex-character fingerprint")
    return normalized


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _new_workdir(path: Path) -> Path:
    candidate = _absolute(path)
    _reject_symlink_components(candidate, include_leaf=False)
    if os.path.lexists(candidate):
        raise InstallerBuildError(
            f"work directory already exists; choose a new path for a safe restart: {candidate}"
        )
    parent = candidate.parent
    if not parent.is_dir() or parent.is_symlink():
        raise InstallerBuildError("work directory parent must be an existing real directory")
    return candidate


def _safe_output(path: Path) -> Path:
    candidate = _absolute(path)
    _reject_symlink_components(candidate, include_leaf=False)
    if os.path.lexists(candidate):
        raise InstallerBuildError(f"output image already exists; choose a new path: {candidate}")
    if not candidate.parent.is_dir() or candidate.parent.is_symlink():
        raise InstallerBuildError("output image parent must be an existing real directory")
    return candidate


def _reject_output_inside_workdir(output: Path, workdir: Path) -> None:
    """Keep the image and build manifest as separate durable artifacts."""

    try:
        output.relative_to(workdir)
    except ValueError:
        return
    raise InstallerBuildError(
        "output image must be outside the build work directory so it cannot overwrite its manifest"
    )


def _json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _json_safe(item) for key, item in asdict(value).items()}
    if isinstance(value, Path):
        return os.fspath(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _write_json(path: Path, document: dict[str, Any]) -> None:
    payload = (json.dumps(_json_safe(document), indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp")
    if os.path.lexists(temporary):
        raise InstallerBuildError(f"build manifest temporary path already exists: {temporary}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
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


def plan(
    *,
    archive: Path,
    signature: Path,
    keyring: Path,
    signer_fingerprint: str,
    hypr_rdp: Path,
    hypr_rdp_sha256: str,
    workdir: Path,
    output: Path,
    archive_sha256: str | None,
    repo_server: str,
    desktop_payload: Path | None = None,
    desktop_payload_sha256: str | None = None,
    installer_source_revision: str | None = None,
) -> dict[str, Any]:
    if installer_source_revision is not None and not re.fullmatch(r"[0-9a-f]{40}", installer_source_revision):
        raise InstallerBuildError("installer source revision must be a full Git commit hash")
    archive_path = _regular_file(archive, name="rootfs archive")
    signature_path = _regular_file(signature, name="rootfs signature")
    keyring_path = _regular_file(keyring, name="trusted keyring")
    binary_path = _regular_file(hypr_rdp, name="hypr-rdp binary")
    fingerprint = _validate_fingerprint(signer_fingerprint)
    binary_digest = _validate_digest(hypr_rdp_sha256, name="hypr-rdp SHA-256")
    expected_archive_digest = None if archive_sha256 is None else _validate_digest(archive_sha256, name="archive SHA-256")
    work_path = _new_workdir(workdir)
    _reject_output_inside_workdir(_absolute(output), work_path)
    output_path = _safe_output(output)
    archive_digest = _sha256(archive_path)
    if expected_archive_digest is not None and archive_digest != expected_archive_digest:
        raise InstallerBuildError("rootfs archive SHA-256 differs from the expected hash")
    binary_digest_actual = _sha256(binary_path)
    if binary_digest_actual != binary_digest:
        raise InstallerBuildError("hypr-rdp binary SHA-256 differs from the expected hash")
    desktop = None
    if (desktop_payload is None) != (desktop_payload_sha256 is None):
        raise InstallerBuildError("desktop payload and expected SHA-256 must be supplied together")
    if desktop_payload is not None:
        desktop = _load_module("desktop_payload.py", "installer_desktop_payload").inspect_bundle(desktop_payload, desktop_payload_sha256)
        desktop["path"] = os.fspath(_absolute(desktop_payload))
    return {
        "schema_version": 1,
        "mode": "plan",
        "build_host": {"architecture": platform.machine().lower(), "native_aarch64": platform.machine().lower() == "aarch64"},
        "inputs": {
            "archive": {"path": os.fspath(archive_path), "sha256": archive_digest},
            "signature": {"path": os.fspath(signature_path), "sha256": _sha256(signature_path)},
            "keyring": {"path": os.fspath(keyring_path), "sha256": _sha256(keyring_path)},
            "expected_signer_fingerprint": fingerprint,
            "hypr_rdp": {"path": os.fspath(binary_path), "sha256": binary_digest_actual},
            "expected_hypr_rdp_sha256": binary_digest,
            "expected_archive_sha256": expected_archive_digest,
            "desktop_payload": desktop,
            "installer_source_revision": installer_source_revision,
        },
        "paths": {"workdir": os.fspath(work_path), "output": os.fspath(output_path)},
        "repository": repo_server,
        "steps": [
            "verify detached rootfs signature and extract into workdir",
            "preview or apply the signed Arch Linux ARM package transaction",
            "stage Pi USB boot policy and generate initramfs in isolated rootfs",
            "stage installer access, network, direct-LAN RDP, and headless session services",
            "assemble a new regular image file",
        ],
        "apply_required": True,
    }


def build(
    *,
    archive: Path,
    signature: Path,
    keyring: Path,
    signer_fingerprint: str,
    hypr_rdp: Path,
    hypr_rdp_sha256: str,
    workdir: Path,
    output: Path,
    archive_sha256: str | None,
    repo_server: str,
    desktop_payload: Path | None = None,
    desktop_payload_sha256: str | None = None,
    installer_source_revision: str | None = None,
) -> dict[str, Any]:
    machine = platform.machine().lower()
    if machine != "aarch64":
        raise InstallerBuildError("image build must run on a native aarch64 build host")
    if os.geteuid() != 0:
        raise InstallerBuildError("image build must run as root")
    specification = plan(
        archive=archive,
        signature=signature,
        keyring=keyring,
        signer_fingerprint=signer_fingerprint,
        hypr_rdp=hypr_rdp,
        hypr_rdp_sha256=hypr_rdp_sha256,
        workdir=workdir,
        output=output,
        archive_sha256=archive_sha256,
        repo_server=repo_server,
        desktop_payload=desktop_payload,
        desktop_payload_sha256=desktop_payload_sha256,
        installer_source_revision=installer_source_revision,
    )
    components = _components()
    work_path = Path(specification["paths"]["workdir"])
    output_path = Path(specification["paths"]["output"])
    prepared = work_path / "prepared-rootfs"
    rootfs = prepared / "rootfs"
    rootfs_manifest = prepared / "rootfs-manifest.json"
    package_manifest = work_path / "package-manifest.json"
    work_path.mkdir(mode=0o700)
    try:
        rootfs_manifest_document = components["rootfs"].prepare_rootfs(
            _absolute(archive),
            _absolute(signature),
            _absolute(keyring),
            _validate_fingerprint(signer_fingerprint),
            prepared,
            expected_archive_sha256=archive_sha256,
        )
        package_manifest_document = components["packages"].stage_packages(
            rootfs,
            rootfs_manifest,
            package_manifest,
            apply=True,
            repo_server=repo_server,
        )
        boot_result = components["boot"].configure_installer_boot(rootfs, generate=True)
        service_result = components["services"].stage_services(
            rootfs,
            _absolute(hypr_rdp),
            _validate_digest(hypr_rdp_sha256, name="hypr-rdp SHA-256"),
            source_revision=installer_source_revision,
        )
        desktop_result = None
        image_arguments = {"mkfs_fat": rootfs / "usr/bin/mkfs.fat"}
        if desktop_payload is not None:
            desktop_result = components["desktop"].stage_bundle(rootfs, desktop_payload, desktop_payload_sha256)
            image_arguments["root_extra_mib"] = (desktop_result["unpacked_bytes"] + 1024 * 1024 - 1) // (1024 * 1024) + 1024
        image_result = components["image"].assemble_image(rootfs, output_path, **image_arguments)
        verification = components["verify"].verify_image(output_path) if desktop_result is not None else None
        build_manifest = {
            "schema_version": 1,
            "mode": "apply",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "build_host": {"architecture": machine, "native_aarch64": True},
            "inputs": specification["inputs"],
            "steps": {
                "rootfs": _json_safe(rootfs_manifest_document),
                "packages": _json_safe(package_manifest_document),
                "boot": _json_safe(boot_result),
                "services": _json_safe(service_result),
                "desktop": desktop_result,
                "image": _json_safe(image_result),
                "verification": _json_safe(verification),
            },
            "artifacts": {
                "rootfs_manifest_sha256": _sha256(rootfs_manifest),
                "package_manifest_sha256": _sha256(package_manifest),
                "image_sha256": _sha256(output_path),
                "image_bytes": output_path.stat().st_size,
            },
        }
        manifest_path = work_path / "build-manifest.json"
        _write_json(manifest_path, build_manifest)
        return build_manifest
    except BaseException:
        # Keep a failed workdir for inspection; the next run must use a new
        # path and cannot accidentally resume over a partial image.
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", "--rootfs-archive", dest="archive", type=Path, required=True)
    parser.add_argument("--signature", "--archive-signature", dest="signature", type=Path, required=True)
    parser.add_argument("--keyring", type=Path, required=True)
    parser.add_argument("--signer-fingerprint", "--expected-fingerprint", dest="signer_fingerprint", required=True)
    parser.add_argument("--archive-sha256", "--expected-archive-sha256", dest="archive_sha256")
    parser.add_argument("--hypr-rdp", type=Path, required=True)
    parser.add_argument("--hypr-rdp-sha256", "--expected-hypr-rdp-sha256", dest="hypr_rdp_sha256", required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo-server", default="https://mirror.archlinuxarm.org/$arch/$repo")
    parser.add_argument("--desktop-payload", type=Path, help="verified step-3 desktop tar.zst")
    parser.add_argument("--desktop-payload-sha256", help="expected desktop bundle SHA-256")
    parser.add_argument("--installer-source-revision", help="full installer source commit; runtime hashes are always recorded")
    parser.add_argument("--apply", action="store_true", help="perform the complete mutating build")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    arguments = {
        "archive": args.archive,
        "signature": args.signature,
        "keyring": args.keyring,
        "signer_fingerprint": args.signer_fingerprint,
        "hypr_rdp": args.hypr_rdp,
        "hypr_rdp_sha256": args.hypr_rdp_sha256,
        "workdir": args.workdir,
        "output": args.output,
        "archive_sha256": args.archive_sha256,
        "repo_server": args.repo_server,
        "desktop_payload": args.desktop_payload,
        "desktop_payload_sha256": args.desktop_payload_sha256,
        "installer_source_revision": args.installer_source_revision,
    }
    try:
        document = build(**arguments) if args.apply else plan(**arguments)
        if not args.apply:
            print(json.dumps(document, indent=2, sort_keys=True))
        else:
            print(f"built installer image: {document['steps']['image']['output']}")
            print(f"build manifest: {_absolute(args.workdir) / 'build-manifest.json'}")
    except Exception as exc:
        print(f"build-installer-image: error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
