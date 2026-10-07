#!/usr/bin/python3
"""Populate a fresh ARM64 root and make a copyable desktop payload.

This is the small step-3 boundary between the package policy and the disk
installer.  It uses the package roots from ``install/arm64/plan.py``, asks
pacman to resolve their dependencies, installs them into an explicitly named
target root, and records the result.  A successful apply produces::

    PAYLOAD/
      rootfs/                    prepared target root (without the pacman cache)
      packages/                  offline package archives and signatures
      desktop-manifest.json      source, roots, versions, and archive hashes

The command does not copy a user's home directory or machine identity.  The
target root must already be a fresh Arch Linux ARM rootfs.  Use the existing
installer-image package staging for the Pi kernel and installer runtime; this
helper owns the desktop transaction only.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Sequence


HERE = Path(__file__).resolve().parent
PLAN_PATH = HERE / "plan.py"
PACKAGE_ARCHIVE = re.compile(r".+\.pkg\.tar\.[A-Za-z0-9]+(?:\.sig)?\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
PACKAGE_NAME = re.compile(r"[A-Za-z0-9@_+][A-Za-z0-9@._+:-]*\Z")
RUNTIME_PACKAGE_NAMES = ("omarchy-settings", "omarchy")


class DesktopPayloadError(RuntimeError):
    """Raised when a desktop payload cannot be made safely."""


def _load_plan() -> Any:
    spec = importlib.util.spec_from_file_location("omarchy_arm64_plan", PLAN_PATH)
    if spec is None or spec.loader is None:
        raise DesktopPayloadError(f"cannot load package plan: {PLAN_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
            raise DesktopPayloadError(f"refusing symlink path component: {current}")


def _real_directory(path: Path, *, name: str, allow_empty: bool = True) -> Path:
    candidate = _absolute(path)
    _reject_symlink_components(candidate)
    try:
        info = candidate.lstat()
    except FileNotFoundError as exc:
        raise DesktopPayloadError(f"{name} does not exist: {candidate}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise DesktopPayloadError(f"{name} must be a real directory: {candidate}")
    if candidate == Path("/"):
        raise DesktopPayloadError(f"refusing the host root as {name}")
    if not allow_empty and not any(candidate.iterdir()):
        raise DesktopPayloadError(f"{name} is empty: {candidate}")
    return candidate


def _regular_file(path: Path, *, name: str) -> Path:
    candidate = _absolute(path)
    _reject_symlink_components(candidate)
    try:
        info = candidate.lstat()
    except FileNotFoundError as exc:
        raise DesktopPayloadError(f"{name} does not exist: {candidate}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size == 0:
        raise DesktopPayloadError(f"{name} must be a nonempty regular file: {candidate}")
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_repo_server(value: str) -> str:
    if any(character.isspace() or ord(character) < 0x20 for character in value):
        raise DesktopPayloadError("repository server contains whitespace or control characters")
    if not value.startswith("https://") or "$arch" not in value or "$repo" not in value:
        raise DesktopPayloadError("repository server must be an HTTPS URL containing $arch and $repo")
    return value


def _render_pacman_config(repo_server: str) -> str:
    return (
        "[options]\n"
        "Architecture = aarch64\n"
        "SigLevel = Required DatabaseOptional\n"
        "LocalFileSigLevel = Optional\n"
        "\n[core]\n"
        f"Server = {repo_server}\n"
        "\n[extra]\n"
        f"Server = {repo_server}\n"
        "\n[alarm]\n"
        f"Server = {repo_server}\n"
    )


def _run(command: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(command, check=check, capture_output=True)
    except OSError as exc:
        raise DesktopPayloadError(f"could not run {command[0]}: {exc.strerror}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode(errors="replace").strip()
        raise DesktopPayloadError(f"{command[0]} failed ({exc.returncode}): {detail or 'no diagnostic'}") from exc


def _plan_document() -> dict[str, Any]:
    module = _load_plan()
    target = {"architecture": "aarch64", "profile": "rpi5", "source": "explicit"}
    try:
        return module.build_plan(target)
    except (OSError, ValueError) as exc:
        raise DesktopPayloadError(f"package policy is invalid: {exc}") from exc


def _select_roots(plan_document: dict[str, Any], profiles: Sequence[str]) -> tuple[list[str], dict[str, list[str]]]:
    if not profiles:
        profiles = ("full-desktop",)
    known_profiles = {"full-desktop", *plan_document["package_profiles"]}
    for name in profiles:
        if name not in known_profiles:
            raise DesktopPayloadError(f"unknown desktop profile: {name}")

    policy = {row["package"]: row for row in plan_document["packages"]}
    selected: list[str] = []
    profile_roots: dict[str, list[str]] = {}
    for name in profiles:
        if name == "full-desktop":
            # build_plan() rejects unclassified upstream packages and exposes
            # only reviewed candidate/replacement rows here. Deferred and
            # excluded entries therefore stay out without a second manifest.
            names = [
                row["package"]
                for row in plan_document["packages"]
                if row["action"] in {"candidate", "replace"}
            ]
        else:
            profile = plan_document["package_profiles"][name]
            names = list(profile["roots"]) + list(profile["file_chooser_roots"])
        translated: list[str] = []
        for package in names:
            row = policy.get(package)
            if row is None:
                raise DesktopPayloadError(f"profile {name} names an unclassified package: {package}")
            if row["action"] == "replace":
                translated.append(row["replacement"])
            elif row["action"] == "candidate":
                translated.append(package)
            else:
                raise DesktopPayloadError(f"profile {name} names non-installable package: {package}")
        profile_roots[name] = list(dict.fromkeys(translated))
        selected.extend(translated)
    return list(dict.fromkeys(selected)), profile_roots


def _parse_resolved(stdout: bytes) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for line in stdout.decode(errors="replace").splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        name, version, repository = fields[:3]
        if not PACKAGE_NAME.fullmatch(name) or not version or any(character.isspace() for character in version):
            continue
        records.append({"name": name, "version": version, "repository": repository})
    if not records:
        raise DesktopPayloadError("pacman did not return resolved package metadata")
    return records


def _parse_info(stdout: bytes) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in stdout.decode(errors="replace").splitlines():
        if not line.strip():
            if current.get("name") and current.get("version"):
                records.append(
                    {
                        "name": current["name"],
                        "version": current["version"],
                        "repository": current.get("repository", "local"),
                    }
                )
            current = {}
            continue
        if " : " not in line:
            continue
        key, value = line.split(" : ", 1)
        key = key.strip()
        if key == "Name":
            current["name"] = value.strip()
        elif key == "Version":
            current["version"] = value.strip()
        elif key == "Repository":
            current["repository"] = value.strip()
    if current.get("name") and current.get("version"):
        records.append(
            {
                "name": current["name"],
                "version": current["version"],
                "repository": current.get("repository", "local"),
            }
        )
    if not records:
        raise DesktopPayloadError("pacman did not return installed package metadata")
    return records


def _validate_custom_archive_metadata(
    pacman: str,
    archive: Path,
    record: dict[str, str],
) -> None:
    """Cross-check the manifest against the archive's native package metadata."""

    result = _run(
        [
            pacman,
            "--query",
            "--file",
            os.fspath(archive),
        ]
    )
    matches: list[tuple[str, str]] = []
    for line in result.stdout.decode(errors="replace").splitlines():
        fields = line.split()
        if len(fields) == 2 and PACKAGE_NAME.fullmatch(fields[0]) and not any(
            character.isspace() for character in fields[1]
        ):
            matches.append((fields[0], fields[1]))
    if len(matches) != 1:
        raise DesktopPayloadError(f"pacman did not return one package record for {archive.name}")
    package, version = matches[0]
    if package != record["package"] or version != record["version"]:
        raise DesktopPayloadError(
            f"custom archive metadata differs from manifest: {archive.name} "
            f"({package} {version}; expected {record['package']} {record['version']})"
        )


def _runtime_package_records(path: Path | None, *, source_revision: str) -> dict[str, dict[str, Any]]:
    """Read the JSON package-pair manifest emitted by build-runtime-packages."""

    if path is None:
        return {}
    manifest = _regular_file(path, name="runtime package manifest")
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DesktopPayloadError("runtime package manifest is not valid JSON") from exc
    if not isinstance(document, dict):
        raise DesktopPayloadError("runtime package manifest must be an object")
    manifest_revision = document.get("source_revision")
    if manifest_revision is None and isinstance(document.get("source"), dict):
        manifest_revision = document["source"].get("revision")
    if not isinstance(manifest_revision, str) or not REVISION.fullmatch(manifest_revision):
        raise DesktopPayloadError("runtime package manifest source revision is invalid")
    if manifest_revision != source_revision:
        raise DesktopPayloadError(
            "runtime package source revision differs from the desktop source: "
            f"{manifest_revision} != {source_revision}"
        )
    rows = document.get("packages")
    if not isinstance(rows, list):
        raise DesktopPayloadError("runtime package manifest has no package records")
    records: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise DesktopPayloadError("runtime package manifest has a malformed package record")
        package = row.get("package") or row.get("name")
        filename = row.get("filename") or row.get("name")
        version = row.get("version")
        architecture = row.get("architecture")
        package_sha256 = row.get("sha256") or row.get("package_sha256")
        if not isinstance(package, str) or not PACKAGE_NAME.fullmatch(package):
            raise DesktopPayloadError("runtime package manifest has an invalid package name")
        if package in records:
            raise DesktopPayloadError(f"runtime package manifest repeats {package}")
        if package not in RUNTIME_PACKAGE_NAMES:
            raise DesktopPayloadError(f"runtime package is not part of the Omarchy pair: {package}")
        if not isinstance(filename, str) or Path(filename).name != filename or not PACKAGE_ARCHIVE.fullmatch(filename):
            raise DesktopPayloadError(f"invalid runtime package filename: {filename!r}")
        if not isinstance(version, str) or not version or any(character.isspace() for character in version):
            raise DesktopPayloadError(f"invalid runtime package version: {package}")
        if architecture != "aarch64":
            raise DesktopPayloadError(f"runtime package {package} is not an aarch64 archive")
        if not isinstance(package_sha256, str) or not SHA256.fullmatch(package_sha256):
            raise DesktopPayloadError(f"runtime package {package} has an invalid archive hash")
        row_revision = row.get("source_revision", manifest_revision)
        if row_revision != source_revision:
            raise DesktopPayloadError(f"runtime package {package} has a mismatching source revision")
        signature = row.get("signature")
        signature_sha256 = row.get("signature_sha256")
        package_signature = row.get("package_signature", "unsigned")
        if package_signature not in {"unsigned", "provided"}:
            raise DesktopPayloadError(f"runtime package {package} has an invalid signature policy")
        if package_signature == "unsigned":
            if signature is not None or signature_sha256 is not None:
                raise DesktopPayloadError(f"unsigned runtime package {package} has signature metadata")
        else:
            if not isinstance(signature, str) or Path(signature).name != signature or not signature.endswith(".sig"):
                raise DesktopPayloadError(f"runtime package {package} has an invalid signature filename")
            if not isinstance(signature_sha256, str) or not SHA256.fullmatch(signature_sha256):
                raise DesktopPayloadError(f"runtime package {package} has an invalid signature hash")
        files = row.get("files")
        if not isinstance(files, list) or not files or any(
            not isinstance(item, str) or Path(item).is_absolute() or ".." in Path(item).parts
            for item in files
        ):
            raise DesktopPayloadError(f"runtime package {package} has invalid ownership records")
        source_sha256 = row.get("source_sha256", document.get("source_sha256"))
        if not isinstance(source_sha256, str) or not SHA256.fullmatch(source_sha256):
            raise DesktopPayloadError(f"runtime package {package} has an invalid source archive hash")
        records[package] = {
            "package": package,
            "name": package,
            "version": version,
            "architecture": architecture,
            "source_revision": source_revision,
            "source_sha256": source_sha256,
            "filename": filename,
            "sha256": package_sha256,
            "package_sha256": package_sha256,
            "package_signature": package_signature,
            "signature": signature,
            "signature_sha256": signature_sha256,
            "files": files,
        }
    if tuple(records) != RUNTIME_PACKAGE_NAMES:
        raise DesktopPayloadError("runtime package manifest must contain omarchy-settings followed by omarchy")
    versions = {record["version"] for record in records.values()}
    if len(versions) != 1:
        raise DesktopPayloadError("runtime package pair has incompatible versions")
    source_hashes = {record["source_sha256"] for record in records.values()}
    if len(source_hashes) != 1:
        raise DesktopPayloadError("runtime package pair has incompatible source hashes")
    return records


def _validate_runtime_archive_metadata(
    pacman: str,
    archive: Path,
    record: dict[str, Any],
) -> None:
    """Require the archive's native pacman metadata to match its pair record."""

    result = _run([pacman, "--query", "--info", "--file", os.fspath(archive)])
    fields: dict[str, str] = {}
    for line in result.stdout.decode(errors="replace").splitlines():
        if " : " in line:
            key, value = line.split(" : ", 1)
            fields[key.strip().lower()] = value.strip()
    if fields.get("name") != record["package"] or fields.get("version") != record["version"]:
        raise DesktopPayloadError(
            f"native runtime package metadata differs from manifest: {archive.name}"
        )
    if fields.get("architecture") != "aarch64":
        raise DesktopPayloadError(f"runtime package archive is not native aarch64: {archive.name}")


def _validate_installed_custom_packages(
    installed: Sequence[dict[str, str]],
    custom: Sequence[Path],
    records: dict[str, dict[str, str]],
) -> None:
    by_name = {item["name"]: item["version"] for item in installed}
    for archive in custom:
        record = records[archive.name]
        if by_name.get(record["package"]) != record["version"]:
            raise DesktopPayloadError(
                f"installed custom package differs from manifest: {record['package']} "
                f"{by_name.get(record['package'], '<missing>')} (expected {record['version']})"
            )


def _validate_installed_runtime_packages(
    installed: Sequence[dict[str, str]],
    records: dict[str, dict[str, Any]],
) -> None:
    by_name = {item["name"]: item["version"] for item in installed}
    for package, record in records.items():
        if by_name.get(package) != record["version"]:
            raise DesktopPayloadError(
                f"installed runtime package differs from manifest: {package} "
                f"{by_name.get(package, '<missing>')} (expected {record['version']})"
            )


def _source_revision(source_checkout: Path | None, source_revision: str | None) -> tuple[str, str]:
    if (source_checkout is None) == (source_revision is None):
        raise DesktopPayloadError("provide exactly one of --source-checkout or --source-revision")
    if source_revision is not None:
        normalized = source_revision.strip().lower()
        if not REVISION.fullmatch(normalized):
            raise DesktopPayloadError("--source-revision must be a full 40-character commit")
        return normalized, "explicit"
    checkout = _real_directory(source_checkout, name="source checkout", allow_empty=False)
    result = _run(["git", "-C", os.fspath(checkout), "rev-parse", "--verify", "HEAD^{commit}"])
    revision = result.stdout.decode(errors="replace").strip().lower()
    if not REVISION.fullmatch(revision):
        raise DesktopPayloadError("source checkout did not resolve to a full commit")
    status = _run(["git", "-C", os.fspath(checkout), "status", "--porcelain", "--untracked-files=all"])
    if status.stdout.strip():
        raise DesktopPayloadError("source checkout must be clean before it is bundled")
    return revision, "git-checkout"


def _account_file(target: Path, name: str) -> Path:
    path = target / "etc" / name
    _reject_symlink_components(path)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise DesktopPayloadError(f"target is missing /etc/{name}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise DesktopPayloadError(f"target /etc/{name} must be a regular file")
    return path


def _lock_stock_root_password(target: Path) -> None:
    """Lock the stock root account in the disposable target template only."""

    path = _account_file(target, "shadow")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DesktopPayloadError("target /etc/shadow is not readable text") from exc
    lines = content.splitlines(keepends=True)
    updated: list[str] = []
    root_count = 0
    for line in lines:
        body = line.rstrip("\r\n")
        suffix = line[len(body) :]
        fields = body.split(":")
        if fields and fields[0] == "root":
            root_count += 1
            if len(fields) < 2:
                raise DesktopPayloadError("target root shadow entry is malformed")
            fields[1] = "!"
            body = ":".join(fields)
        updated.append(body + suffix)
    if root_count != 1:
        raise DesktopPayloadError("target must contain exactly one root shadow entry")
    new_content = "".join(updated)
    if new_content != content:
        path.write_text(new_content, encoding="utf-8")


def _remove_account_backups(target: Path) -> None:
    """Remove account-file backups created while deleting stock accounts."""

    for name in ("passwd-", "shadow-", "group-", "gshadow-"):
        path = target / "etc" / name
        _reject_symlink_components(path)
        if not os.path.lexists(path):
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise DesktopPayloadError(f"target /etc/{name} must not be a special file")
        path.unlink()


def _validate_generic_accounts(target: Path) -> None:
    """Reject shipped login identities and require a locked root account."""

    passwd_path = _account_file(target, "passwd")
    shadow_path = _account_file(target, "shadow")
    gshadow_path = _account_file(target, "gshadow")
    try:
        passwd_lines = passwd_path.read_text(encoding="utf-8").splitlines()
        shadow_lines = shadow_path.read_text(encoding="utf-8").splitlines()
        gshadow_lines = gshadow_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise DesktopPayloadError("target account files are not readable text") from exc

    passwd_names: set[str] = set()
    for line in passwd_lines:
        if not line:
            continue
        fields = line.split(":")
        if len(fields) != 7 or not fields[2].isdigit():
            raise DesktopPayloadError("target has an invalid passwd entry")
        name = fields[0]
        if name in passwd_names:
            raise DesktopPayloadError(f"target has a duplicate passwd entry: {name}")
        passwd_names.add(name)
        uid = int(fields[2])
        if name == "alarm" or 1000 <= uid < 65534:
            raise DesktopPayloadError(f"target has a non-system account: {name}")
    if "root" not in passwd_names:
        raise DesktopPayloadError("target is missing the root account")

    root_shadow: list[list[str]] = []
    for line in shadow_lines:
        if not line:
            continue
        fields = line.split(":")
        if len(fields) != 9:
            raise DesktopPayloadError("target has an invalid shadow entry")
        if fields[0] == "root":
            root_shadow.append(fields)
        if fields[0] == "alarm":
            raise DesktopPayloadError("target has an alarm shadow entry")
    if len(root_shadow) != 1 or not root_shadow[0][1].startswith(("!", "*")):
        raise DesktopPayloadError("target root account is not locked")

    for line in gshadow_lines:
        if not line:
            continue
        fields = line.split(":")
        if len(fields) != 4:
            raise DesktopPayloadError("target has an invalid gshadow entry")
        if fields[0] == "alarm":
            raise DesktopPayloadError("target has an alarm gshadow entry")

    for name in ("passwd-", "shadow-", "group-", "gshadow-"):
        path = target / "etc" / name
        _reject_symlink_components(path)
        if os.path.lexists(path):
            raise DesktopPayloadError(f"target retains the account backup /etc/{name}")


def _has_stock_alarm_account(target: Path) -> bool:
    """Recognize only the untouched login shipped in the signed ALARM tarball."""

    passwd = (target / "etc/passwd").read_text(encoding="utf-8")
    accounts = [line.split(":") for line in passwd.splitlines()]
    for account in accounts:
        if len(account) != 7 or not account[2].isdigit():
            raise DesktopPayloadError("target has an invalid passwd entry")
        if 1000 <= int(account[2]) < 65534 and account[0] != "alarm":
            raise DesktopPayloadError("target has a pre-existing non-system account")
    entries = [account for account in accounts if account[0] == "alarm"]
    if not entries:
        return False
    if len(entries) != 1 or len(entries[0]) != 7 or entries[0][2:4] != ["1000", "1000"]:
        raise DesktopPayloadError("target has a nonstandard alarm account")
    if entries[0][5:] != ["/home/alarm", "/bin/bash"]:
        raise DesktopPayloadError("target has a nonstandard alarm home or shell")
    alarm_home = target / "home/alarm"
    if alarm_home.is_symlink() or not alarm_home.is_dir():
        raise DesktopPayloadError("target has an invalid stock alarm home")
    files = {item.name for item in alarm_home.iterdir()}
    if files != {".bash_profile", ".bash_logout", ".bashrc"}:
        raise DesktopPayloadError("target alarm home contains nonstandard files")
    if any(not item.is_file() or item.is_symlink() for item in alarm_home.iterdir()):
        raise DesktopPayloadError("target alarm home contains non-regular files")
    return True


def _validate_generic_root(target: Path, *, allow_stock_alarm: bool = False) -> None:
    """Reject identity and private data before making a generic payload."""

    machine_id = target / "etc/machine-id"
    if machine_id.is_symlink():
        raise DesktopPayloadError("target root has a symlink machine-id; use a fresh generic root")
    elif machine_id.exists() and machine_id.stat().st_size:
        raise DesktopPayloadError("target root has a machine-id; use a fresh generic root")

    hostname = target / "etc/hostname"
    if hostname.is_symlink():
        raise DesktopPayloadError("target root has a symlink hostname")
    if hostname.exists() and hostname.read_text(encoding="utf-8") not in ("", "alarm\n"):
        raise DesktopPayloadError("target root has a configured hostname; use a fresh generic root")

    ssh = target / "etc/ssh"
    if ssh.is_dir() and not ssh.is_symlink():
        for item in ssh.glob("ssh_host_*"):
            if item.is_symlink() or (item.is_file() and item.stat().st_size):
                raise DesktopPayloadError("target root has SSH host keys; use a fresh generic root")

    for home in (target / "home", target / "root"):
        if not home.exists():
            continue
        if home.is_symlink() or not home.is_dir():
            raise DesktopPayloadError(f"target root has an invalid home directory: {home}")
        entries = list(home.iterdir())
        if home.name == "root" and all(
            entry.name in {".ssh", ".cache"}
            and entry.is_dir()
            and not entry.is_symlink()
            and not any(entry.iterdir())
            for entry in entries
        ):
            # Package hooks can create an empty root cache directory. Neither
            # empty directory contains login identity or user data.
            continue
        if home.name == "home" and allow_stock_alarm and len(entries) == 1:
            if entries[0].name == "alarm" and _has_stock_alarm_account(target):
                continue
        if entries:
            raise DesktopPayloadError(f"target root has user files in {home}; use a fresh generic root")


def _custom_manifest_records(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    candidate = _absolute(path)
    if candidate.is_dir() and not candidate.is_symlink():
        manifest_path = _regular_file(candidate / "manifest.tsv", name="custom package manifest")
    else:
        manifest_path = _regular_file(candidate, name="custom package manifest")
    result: dict[str, dict[str, str]] = {}
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise DesktopPayloadError("custom package manifest is not readable text") from exc
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 8:
            raise DesktopPayloadError("custom package manifest must use the eight-column manifest.tsv format")
        package, version, architecture, source_revision, source_sha256, package_sha256, signature, filename = fields
        if architecture != "aarch64" or signature != "unsigned" or not filename or Path(filename).name != filename:
            raise DesktopPayloadError(f"invalid custom package manifest record: {filename}")
        if not SHA256.fullmatch(package_sha256) or not SHA256.fullmatch(source_sha256):
            raise DesktopPayloadError(f"invalid custom package hash in manifest: {filename}")
        if filename in result:
            raise DesktopPayloadError(f"duplicate custom package archive in manifest: {filename}")
        result[filename] = {
            "sha256": package_sha256,
            "package": package,
            "version": version,
            "source_revision": source_revision,
            "source_sha256": source_sha256,
        }
    return result


def _pacman_base(
    pacman: str,
    rootfs: Path,
    dbpath: Path,
    cachedir: Path,
    logfile: Path,
    config: Path,
    gpgdir: Path,
    hookdir: Path,
) -> list[str]:
    return [
        pacman,
        "--root",
        os.fspath(rootfs),
        "--dbpath",
        os.fspath(dbpath),
        "--cachedir",
        os.fspath(cachedir),
        "--logfile",
        os.fspath(logfile),
        "--config",
        os.fspath(config),
        "--gpgdir",
        os.fspath(gpgdir),
        "--hookdir",
        os.fspath(hookdir),
    ]


def _new_output(path: Path) -> Path:
    candidate = _absolute(path)
    _reject_symlink_components(candidate, include_leaf=False)
    if os.path.lexists(candidate):
        raise DesktopPayloadError(f"refusing to overwrite payload output: {candidate}")
    if not candidate.parent.is_dir() or candidate.parent.is_symlink():
        raise DesktopPayloadError("payload output parent must be an existing real directory")
    return candidate


def _archive_files(cache: Path, custom: Sequence[Path]) -> list[tuple[Path, str]]:
    candidates: dict[str, Path] = {}
    if cache.exists():
        if cache.is_symlink() or not cache.is_dir():
            raise DesktopPayloadError("target pacman cache is not a real directory")
        for item in sorted(cache.iterdir()):
            if item.is_file() and not item.is_symlink() and PACKAGE_ARCHIVE.fullmatch(item.name):
                candidates[item.name] = item
    for item in custom:
        path = _regular_file(item, name="custom package archive")
        if not PACKAGE_ARCHIVE.fullmatch(path.name):
            raise DesktopPayloadError(f"custom package is not a pacman archive: {path.name}")
        existing = candidates.get(path.name)
        if existing is not None and _sha256(existing) != _sha256(path):
            raise DesktopPayloadError(f"package archive name collision with different bytes: {path.name}")
        candidates[path.name] = path
    if not candidates:
        raise DesktopPayloadError("pacman produced no package archives for the offline bundle")
    return [(path, origin) for name, path in sorted(candidates.items()) for origin in ("custom" if path in custom else "pacman-cache",)]


def _copy_tree_without_cache(source: Path, destination: Path) -> None:
    def ignore(path: str, names: list[str]) -> set[str]:
        if Path(path) == source / "var/cache/pacman":
            return {"pkg"}
        if Path(path) == source / "etc/pacman.d":
            # Signed staging generates a target-local private master key.
            # Each installed machine must initialize its own pacman keyring.
            return {"gnupg"}
        return set()

    shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2, ignore=ignore)
    # copy2 preserves permissions but not ownership. Keep service-owned paths
    # usable, and restore mode bits if changing an owner cleared setgid/setuid.
    for root, dirs, files in os.walk(destination, followlinks=False):
        for copied in (Path(root), *(Path(root) / name for name in files + dirs)):
            original = source / copied.relative_to(destination)
            owner = original.lstat()
            current = copied.lstat()
            if (current.st_uid, current.st_gid) != (owner.st_uid, owner.st_gid):
                os.chown(copied, owner.st_uid, owner.st_gid, follow_symlinks=False)
                shutil.copystat(original, copied, follow_symlinks=False)


def _seed_packaged_runtime_state(
    target: Path,
    runtime: Sequence[Path],
    records: dict[str, dict[str, Any]],
    *,
    source_revision: str,
) -> None:
    """Retain the installed pair and marker needed for first update rollback."""

    runtime_root = target / "usr/share/omarchy"
    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise DesktopPayloadError("packaged Omarchy runtime is missing from the target")
    marker = runtime_root / ".omarchy-pi-packaged.json"
    marker_document = {
        "schema_version": 1,
        "runtime_mode": "packaged",
        "source_revision": source_revision,
        "packages": [
            {
                "name": record["package"],
                "version": record["version"],
                "architecture": record["architecture"],
                "filename": record["filename"],
                "sha256": record["sha256"],
                "package_signature": record["package_signature"],
            }
            for record in records.values()
        ],
    }
    marker_text = json.dumps(marker_document, indent=2, sort_keys=True) + "\n"
    if marker.exists() or marker.is_symlink():
        try:
            existing_marker = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DesktopPayloadError("packaged runtime marker is not valid JSON") from exc
        if (
            marker.is_symlink()
            or not isinstance(existing_marker, dict)
            or existing_marker.get("runtime_mode", existing_marker.get("mode")) != "packaged"
            or existing_marker.get("source_revision") != source_revision
        ):
            raise DesktopPayloadError("packaged runtime marker differs from the selected package pair")
    else:
        marker.write_text(marker_text, encoding="utf-8")
        marker.chmod(0o644)
    if os.geteuid() == 0:
        os.chown(marker, 0, 0)

    rollback = target / "usr/share/omarchy-pi/rollback"
    _reject_symlink_components(rollback, include_leaf=False)
    if rollback.exists() or rollback.is_symlink():
        if rollback.is_symlink() or not rollback.is_dir():
            raise DesktopPayloadError("packaged runtime rollback directory is unsafe")
    else:
        rollback.mkdir(mode=0o750, parents=True)
    rollback_records: list[dict[str, Any]] = []
    records_by_filename = {record["filename"]: record for record in records.values()}
    for archive in runtime:
        record = records_by_filename[archive.name]
        destination = rollback / archive.name
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or _sha256(destination) != record["sha256"]:
                raise DesktopPayloadError(f"packaged runtime rollback archive differs: {archive.name}")
        else:
            shutil.copy2(archive, destination)
            destination.chmod(0o640)
        package_record = {
            "name": record["package"],
            "version": record["version"],
            "architecture": record["architecture"],
            "filename": archive.name,
            "sha256": record["sha256"],
            "signature": "required" if record["package_signature"] == "provided" else "optional",
        }
        if record["package_signature"] == "provided":
            signature_source = archive.parent / record["signature"]
            signature_destination = rollback / signature_source.name
            if signature_destination.is_symlink() or (signature_destination.exists() and _sha256(signature_destination) != record["signature_sha256"]):
                raise DesktopPayloadError(f"packaged runtime rollback signature differs: {signature_source.name}")
            if not signature_destination.exists():
                shutil.copy2(signature_source, signature_destination)
                signature_destination.chmod(0o640)
            package_record["signature_file"] = signature_source.name
        rollback_records.append(package_record)
    rollback_manifest = {
        "schema_version": 1,
        "architecture": "aarch64",
        "version": next(iter(records.values()))["version"],
        "source_revision": source_revision,
        "packages": rollback_records,
    }
    manifest_path = rollback / "manifest.json"
    manifest_text = json.dumps(rollback_manifest, indent=2, sort_keys=True) + "\n"
    if manifest_path.exists() or manifest_path.is_symlink():
        if manifest_path.is_symlink() or manifest_path.read_text(encoding="utf-8") != manifest_text:
            raise DesktopPayloadError("packaged runtime rollback manifest differs from the selected pair")
    else:
        manifest_path.write_text(manifest_text, encoding="utf-8")
        manifest_path.chmod(0o640)
    if os.geteuid() == 0:
        for path in (rollback, *rollback.iterdir()):
            os.chown(path, 0, 0)


def _clone_source(source: Path, destination: Path, revision: str) -> None:
    """Bundle a shallow, origin-free checkout without host Git metadata."""

    _run(
        [
            "git",
            "clone",
            "--no-local",
            "--no-hardlinks",
            "--no-tags",
            "--single-branch",
            "--depth",
            "1",
            source.as_uri(),
            os.fspath(destination),
        ]
    )
    _run(["git", "-C", os.fspath(destination), "checkout", "--detach", revision])
    _run(["git", "-C", os.fspath(destination), "remote", "remove", "origin"])


def _validate_tree(source: Path) -> None:
    for root, dirs, files in os.walk(source, followlinks=False):
        for name in (*dirs, *files):
            candidate = Path(root) / name
            mode = candidate.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                raise DesktopPayloadError(f"target root contains unsupported special file: {candidate}")


def build_payload(
    *,
    rootfs: Path,
    output: Path,
    profiles: Sequence[str],
    source_checkout: Path | None,
    source_revision: str | None,
    custom_packages: Sequence[Path] = (),
    custom_package_manifest: Path | None = None,
    runtime_packages: Sequence[Path] = (),
    runtime_package_manifest: Path | None = None,
    provisioner: Path | None = None,
    repo_server: str = "https://ca.us.mirror.archlinuxarm.org/$arch/$repo",
    pacman: str = "pacman",
    apply: bool = True,
    require_native_aarch64: bool = True,
) -> dict[str, Any]:
    if require_native_aarch64 and platform.machine().lower() != "aarch64":
        raise DesktopPayloadError("desktop package staging must run on a native aarch64 build host")
    target = _real_directory(rootfs, name="target root", allow_empty=False)
    destination = _new_output(output)
    repository = _validate_repo_server(repo_server)
    if source_checkout is not None:
        source_checkout = _absolute(source_checkout)
    revision, revision_source = _source_revision(source_checkout, source_revision)
    if apply and source_checkout is None:
        raise DesktopPayloadError("--apply requires --source-checkout so the clean source can be bundled")
    if apply and provisioner is None:
        provisioner = source_checkout / "install/arm64/provision-desktop-root.sh"
    stock_alarm = _has_stock_alarm_account(target)
    _validate_generic_root(target, allow_stock_alarm=stock_alarm)
    hostname_path = target / "etc/hostname"
    stock_hostname = hostname_path.exists() and hostname_path.read_text(encoding="utf-8") == "alarm\n"
    plan_document = _plan_document()
    roots, profile_roots = _select_roots(plan_document, profiles)
    custom = [_regular_file(path, name="custom package archive") for path in custom_packages]
    custom_records = _custom_manifest_records(custom_package_manifest)
    if custom and not custom_records:
        raise DesktopPayloadError("custom package archives require --custom-package-manifest")
    for archive in custom:
        record = custom_records.get(archive.name)
        if record is None or record["sha256"] != _sha256(archive):
            raise DesktopPayloadError(f"custom package is not recorded with its expected hash: {archive.name}")
        _validate_custom_archive_metadata(pacman, archive, record)
    runtime = [_regular_file(path, name="runtime package archive") for path in runtime_packages]
    runtime_records = _runtime_package_records(runtime_package_manifest, source_revision=revision)
    if runtime and not runtime_records:
        raise DesktopPayloadError("runtime package archives require --runtime-package-manifest")
    if runtime_records and not runtime:
        raise DesktopPayloadError("runtime package manifest requires --runtime-package archives")
    runtime_by_filename = {record["filename"]: record for record in runtime_records.values()}
    runtime_by_package = {record["package"]: record for record in runtime_records.values()}
    if len(runtime) != len(runtime_records):
        raise DesktopPayloadError("runtime package archives must contain exactly the manifest pair")
    for archive in runtime:
        record = runtime_by_filename.get(archive.name)
        if record is None:
            raise DesktopPayloadError(f"runtime package is absent from its manifest: {archive.name}")
        if record["sha256"] != _sha256(archive):
            raise DesktopPayloadError(f"runtime package is not recorded with its expected hash: {archive.name}")
        _validate_runtime_archive_metadata(pacman, archive, record)
        if record["package_signature"] == "provided":
            signature = archive.parent / record["signature"]
            _regular_file(signature, name=f"signature for {record['package']}")
            if _sha256(signature) != record["signature_sha256"]:
                raise DesktopPayloadError(f"runtime package signature hash differs from manifest: {signature.name}")
    if set(runtime_by_package) != set(RUNTIME_PACKAGE_NAMES):
        raise DesktopPayloadError("runtime package manifest must contain the complete Omarchy pair")
    all_custom = [*custom, *runtime]
    all_custom_names = [archive.name for archive in all_custom]
    if len(set(all_custom_names)) != len(all_custom_names):
        raise DesktopPayloadError("custom and runtime package archives contain a duplicate filename")
    selected_profiles = profiles or ("full-desktop",)
    if "full-desktop" in selected_profiles:
        required_custom = {"hypr-rdp", "ttfx"}
        supplied_custom = {
            custom_records.get(archive.name, {}).get("package") for archive in custom
        }
        missing_custom = sorted(required_custom - supplied_custom)
        if missing_custom:
            raise DesktopPayloadError(
                "full-desktop requires recorded custom packages: " + ", ".join(missing_custom)
            )
        # ttfx is intentionally built locally.  Remove it from the sync
        # roots so a fresh ALARM database does not try to find a repo package
        # before the reviewed archive is installed below.
        roots = [root for root in roots if root not in supplied_custom]

    dbpath = target / "var/lib/pacman"
    cache = target / "var/cache/pacman/pkg"
    gpgdir = target / "etc/pacman.d/gnupg"
    hookdir = target / "etc/pacman.d/hooks"
    _reject_symlink_components(dbpath)
    _reject_symlink_components(cache, include_leaf=False)
    _reject_symlink_components(gpgdir, include_leaf=False)
    _reject_symlink_components(hookdir, include_leaf=False)
    _reject_symlink_components(log := target / "var/log/pacman-desktop.log", include_leaf=False)
    if not dbpath.is_dir() or dbpath.is_symlink():
        raise DesktopPayloadError("target root is missing var/lib/pacman")
    if not gpgdir.is_dir() or gpgdir.is_symlink():
        raise DesktopPayloadError("target root is missing its pacman GPG directory; run signed base staging first")
    if apply:
        if stock_alarm:
            # The official tarball includes the default alarm login. Remove
            # it from this disposable target before bundling a generic image.
            _lock_stock_root_password(target)
            _run(["userdel", "--root", os.fspath(target), "--remove", "alarm"])
            groups = (target / "etc/group").read_text(encoding="utf-8").splitlines()
            if any(line.startswith("alarm:") for line in groups):
                _run(["groupdel", "--root", os.fspath(target), "alarm"])
            _remove_account_backups(target)
            _validate_generic_root(target)
        if stock_hostname:
            hostname_path.write_text("", encoding="utf-8")
        cache.mkdir(parents=True, exist_ok=True)
        hookdir.mkdir(parents=True, exist_ok=True)
        log.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="omarchy-pi-desktop-") as temporary:
        temporary_root = Path(temporary)
        config = temporary_root / "pacman.conf"
        config.write_text(_render_pacman_config(repository), encoding="utf-8")
        preview_db = temporary_root / "db"
        shutil.copytree(dbpath, preview_db, symlinks=True)
        preview_gpgdir = temporary_root / "gpgdir"
        shutil.copytree(gpgdir, preview_gpgdir, symlinks=True)
        preview_cache = temporary_root / "cache"
        preview_cache.mkdir()
        preview_hookdir = temporary_root / "hooks"
        preview_hookdir.mkdir()
        preview_log = temporary_root / "pacman.log"
        preview_command = _pacman_base(
            pacman, target, preview_db, preview_cache, preview_log, config, preview_gpgdir, preview_hookdir
        )
        preview_command.extend(
            ["--sync", "--refresh", "--print", "--print-format", "%n\t%v\t%r", "--needed", *roots]
        )
        resolved_result = _run(preview_command)
        resolved = _parse_resolved(resolved_result.stdout)

        installed: list[dict[str, str]] | None = None
        if apply:
            apply_command = _pacman_base(pacman, target, dbpath, cache, log, config, gpgdir, hookdir)
            apply_command.extend(["--sync", "--refresh", "--needed", "--noconfirm", *roots])
            _run(apply_command)
            if all_custom:
                custom_command = _pacman_base(pacman, target, dbpath, cache, log, config, gpgdir, hookdir)
                custom_command.extend(["--upgrade", "--needed", "--noconfirm", *(os.fspath(path) for path in all_custom)])
                _run(custom_command)
            provisioner_path = _regular_file(provisioner, name="desktop provisioner")
            provision_command = [
                os.fspath(provisioner_path),
                "--rootfs",
                os.fspath(target),
                "--source-checkout",
                os.fspath(source_checkout),
            ]
            if runtime_records:
                provision_command.extend(["--runtime-layout", "packaged"])
            if not os.access(provisioner_path, os.X_OK):
                provision_command.insert(0, "bash")
            _run(provision_command)
            query_command = _pacman_base(pacman, target, dbpath, cache, log, config, gpgdir, hookdir)
            query_command.extend(["--query", "--info"])
            installed = _parse_info(_run(query_command).stdout)
            _validate_installed_custom_packages(installed, custom, custom_records)
            _validate_installed_runtime_packages(installed, runtime_records)
            if runtime_records:
                _seed_packaged_runtime_state(
                    target,
                    runtime,
                    runtime_records,
                    source_revision=revision,
                )

        manifest: dict[str, Any] = {
            "schema_version": 1,
            "mode": "apply" if apply else "preview",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": {"revision": revision, "revision_source": revision_source},
            "target": {"architecture": "aarch64", "profile": "rpi5", "rootfs": target.name},
            "stock_alarm_account": "removed" if apply and stock_alarm else ("present; apply removes it" if stock_alarm else "absent"),
            "stock_hostname": "cleared" if apply and stock_hostname else ("present; apply clears it" if stock_hostname else "absent"),
            "profiles": {"selected": list(profiles) or ["full-desktop"], "roots": profile_roots},
            "transaction": {
                "requested_roots": roots,
                "resolved_packages": resolved,
                "installed_packages": installed,
                "custom_archives": [
                    {"name": path.name, **custom_records[path.name]} for path in custom
                ],
                "runtime_archives": [
                    {**runtime_by_filename[path.name]} for path in runtime
                ],
            },
            "payload": {
                "rootfs": "rootfs",
                "packages": "packages",
                "manifest": "desktop-manifest.json",
                "source": "source",
                "excluded_build_state": ["var/cache/pacman/pkg", "etc/pacman.d/gnupg"],
                "offline_archives": [],
            },
            "policy": {"baseline": plan_document["baseline"], "plan_schema_version": plan_document["schema_version"]},
        }
        if runtime_records:
            manifest["runtime"] = {
                "layout": "packaged",
                "path": "/usr/share/omarchy",
                "source_revision": revision,
                "packages": [
                    {
                        "name": record["package"],
                        "package": record["package"],
                        "version": record["version"],
                        "architecture": record["architecture"],
                        "filename": record["filename"],
                        "sha256": record["sha256"],
                        "source_revision": record["source_revision"],
                        "source_sha256": record["source_sha256"],
                        "package_signature": record["package_signature"],
                        "signature": record["signature"],
                        "signature_sha256": record["signature_sha256"],
                        "files": record["files"],
                    }
                    for record in runtime_records.values()
                ],
            }
        manifest["policy"]["signature_policy"] = {
            "repository_packages": "Required",
            "repository_databases": "Optional",
            "custom_archives": "Unsigned; accepted only after manifest SHA-256/source pin validation",
            "runtime_archives": "LocalFileSigLevel Optional; package pair/source/hash validation is mandatory",
        }
        if not apply:
            return manifest

        _remove_account_backups(target)
        _validate_generic_root(target)
        _validate_generic_accounts(target)
        _validate_tree(target)
        archives = _archive_files(cache, all_custom)
        destination.mkdir(mode=0o755)
        package_dir = destination / "packages"
        package_dir.mkdir()
        archive_manifest: list[dict[str, Any]] = []
        custom_paths = set(all_custom)
        for source, origin in archives:
            destination_archive = package_dir / source.name
            shutil.copy2(source, destination_archive)
            archive_manifest.append(
                {
                    "name": source.name,
                    "bytes": destination_archive.stat().st_size,
                    "sha256": _sha256(destination_archive),
                    "origin": "custom" if source in custom_paths else origin,
                }
            )
        for record in runtime_records.values():
            if record["package_signature"] != "provided":
                continue
            signature_source = next(
                archive.parent / record["signature"] for archive in runtime if archive.name == record["filename"]
            )
            signature_destination = package_dir / signature_source.name
            if signature_destination.exists():
                if _sha256(signature_destination) != record["signature_sha256"]:
                    raise DesktopPayloadError(f"runtime package signature collision: {signature_destination.name}")
                continue
            shutil.copy2(signature_source, signature_destination)
            archive_manifest.append(
                {
                    "name": signature_destination.name,
                    "bytes": signature_destination.stat().st_size,
                    "sha256": _sha256(signature_destination),
                    "origin": "runtime-signature",
                }
            )
        root_destination = destination / "rootfs"
        _copy_tree_without_cache(target, root_destination)
        if source_checkout is None:
            raise DesktopPayloadError("source checkout disappeared before payload copy")
        _clone_source(source_checkout, destination / "source", revision)
        manifest["payload"]["offline_archives"] = archive_manifest
        manifest_path = destination / "desktop-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rootfs", type=Path, required=True, help="fresh target root to populate")
    parser.add_argument("--output", type=Path, required=True, help="new desktop payload directory")
    parser.add_argument("--profile", action="append", dest="profiles", help="desktop profile (repeatable; default full-desktop)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-checkout", type=Path, help="checkout whose HEAD is recorded")
    source.add_argument("--source-revision", help="full 40-character source commit")
    parser.add_argument("--custom-package", action="append", default=[], type=Path, help="prebuilt .pkg.tar.* archive (repeatable)")
    parser.add_argument("--custom-package-manifest", type=Path, help="manifest recording custom archive hashes and package names")
    parser.add_argument("--runtime-package", action="append", default=[], type=Path, help="Omarchy runtime package archive (repeatable)")
    parser.add_argument("--runtime-package-manifest", type=Path, help="JSON manifest recording the matching Omarchy runtime package pair")
    parser.add_argument("--provisioner", type=Path, help="generic root provisioner (default: the one in --source-checkout)")
    parser.add_argument("--repo-server", default="https://ca.us.mirror.archlinuxarm.org/$arch/$repo")
    parser.add_argument("--pacman", default="pacman", help=argparse.SUPPRESS)
    parser.add_argument("--preview", action="store_true", help="resolve in a scratch database without changing the target or writing a payload")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = build_payload(
            rootfs=args.rootfs,
            output=args.output,
            profiles=args.profiles or ("full-desktop",),
            source_checkout=args.source_checkout,
            source_revision=args.source_revision,
            custom_packages=args.custom_package,
            custom_package_manifest=args.custom_package_manifest,
            runtime_packages=args.runtime_package,
            runtime_package_manifest=args.runtime_package_manifest,
            provisioner=args.provisioner,
            repo_server=args.repo_server,
            pacman=args.pacman,
            apply=not args.preview,
        )
    except (DesktopPayloadError, OSError, subprocess.CalledProcessError) as exc:
        print(f"build-desktop-payload: error: {exc}", file=sys.stderr)
        return 2
    if args.preview:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"desktop payload: {_absolute(args.output)}")
        print(f"manifest: {_absolute(args.output) / 'desktop-manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
