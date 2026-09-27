#!/usr/bin/python3
"""Stage the bounded Arch Linux ARM package transaction into a rootfs tree.

The command is a build-host operation.  It always supplies pacman with an
explicit target root, package database, cache, log, hook directory and target
keyring.  Preview mode uses a scratch copy of the target package database and
cache so it does not mutate the staged root.  ``--apply`` is the explicit
mode that writes the target package database and rootfs.

This step only resolves the requested runtime roots and their dependencies,
plus the Pi-native kernel and bootloader replacements.  Boot configuration and
initramfs generation are separate steps.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
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
from urllib.parse import urlparse


class PackageStageError(RuntimeError):
    """Raised when package staging cannot be kept inside the target root."""


RUNTIME_ROOTS = (
    "networkmanager",
    # The Pi country setup uses iw reg set; NetworkManager does not guarantee
    # the command as an explicit dependency.
    "iw",
    "openssh",
    "sudo",
    "python",
    "hyprland",
    "uwsm",
    "foot",
    "qt6-wayland",
    "xdg-terminal-exec",
    "dosfstools",
    # The later hypr-rdp step links against libpipewire-0.3; keep the
    # provider in this signed runtime transaction even though Hyprland does
    # not pull it in by itself.
    "pipewire",
)
PI_BOOT_PACKAGES = ("linux-rpi", "raspberrypi-bootloader")
GENERIC_BOOT_PACKAGES = ("linux-aarch64", "uboot-raspberrypi")
TRANSACTION_TARGETS = PI_BOOT_PACKAGES + RUNTIME_ROOTS
PACMAN_PRINT_FORMAT = "%n\t%v\t%r"
NSPAWN_CONFIG_PATH = "/run/omarchy-pi-pacman.conf"
GPGCONF_COMMAND = "gpgconf"
DEFAULT_REPO_SERVER = "https://mirror.archlinuxarm.org/$arch/$repo"
PACKAGE_NAME = re.compile(r"[A-Za-z0-9@_+][A-Za-z0-9@._+:-]*\Z")
VERSION = re.compile(r"[^\s]+\Z")
REPO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.-]*\Z")


def _absolute_lexical(path: Path) -> Path:
    if path.is_absolute():
        return Path(os.path.normpath(os.fspath(path)))
    return Path(os.path.normpath(os.path.join(os.getcwd(), os.fspath(path))))


def _reject_symlink_components(path: Path, *, include_leaf: bool = True) -> None:
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
            raise PackageStageError(f"refusing symlink path component: {current}")


def _require_regular_file(path: Path, *, name: str, nonempty: bool = True) -> Path:
    candidate = _absolute_lexical(path)
    _reject_symlink_components(candidate)
    try:
        info = candidate.lstat()
    except FileNotFoundError as exc:
        raise PackageStageError(f"{name} does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PackageStageError(f"{name} must be a regular file")
    if nonempty and info.st_size == 0:
        raise PackageStageError(f"{name} is empty")
    return candidate


def _require_target_root(path: Path) -> Path:
    candidate = _absolute_lexical(path)
    _reject_symlink_components(candidate)
    try:
        info = candidate.lstat()
    except FileNotFoundError as exc:
        raise PackageStageError("target root does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PackageStageError("target root must be a real directory")
    resolved = candidate.resolve(strict=True)
    if resolved == Path("/"):
        raise PackageStageError("refusing the host root as the package target")
    for protected in (Path("/dev"), Path("/proc"), Path("/sys"), Path("/run"), Path("/boot")):
        try:
            resolved.relative_to(protected)
        except ValueError:
            continue
        raise PackageStageError(f"refusing a target root under {protected}")
    try:
        next(resolved.iterdir())
    except StopIteration as exc:
        raise PackageStageError("target root is empty") from exc
    return resolved


def _target_child(target: Path, relative: str, *, required_dir: bool = False, required_file: bool = False) -> Path:
    candidate = target / relative
    _reject_symlink_components(candidate)
    if required_dir:
        if not candidate.is_dir():
            raise PackageStageError(f"target is missing directory {relative}")
    if required_file:
        if not candidate.is_file():
            raise PackageStageError(f"target is missing file {relative}")
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_rootfs_manifest(path: Path, rootfs: Path) -> tuple[dict[str, Any], str]:
    manifest_path = _require_regular_file(path, name="rootfs manifest")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageStageError("rootfs manifest is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise PackageStageError("rootfs manifest has an unsupported schema")
    staging = document.get("staging")
    if not isinstance(staging, dict) or staging.get("rootfs_directory") != rootfs.name:
        raise PackageStageError("rootfs manifest does not identify this rootfs directory")
    source = document.get("source")
    if not isinstance(source, dict):
        raise PackageStageError("rootfs manifest has no source metadata")
    for key in ("archive_sha256", "signer_fingerprint"):
        if not isinstance(source.get(key), str) or not source[key]:
            raise PackageStageError("rootfs manifest has incomplete source metadata")
    return document, _sha256_file(manifest_path)


def _validate_repo_server(server: str) -> str:
    if any(character.isspace() or ord(character) < 0x20 for character in server):
        raise PackageStageError("repository server contains whitespace or control characters")
    parsed = urlparse(server)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PackageStageError("repository server must be an HTTPS URL without credentials or query data")
    hostname = (parsed.hostname or "").lower()
    if hostname != "archlinuxarm.org" and not hostname.endswith(".archlinuxarm.org"):
        raise PackageStageError("repository server must be under archlinuxarm.org")
    if "$arch" not in parsed.path or "$repo" not in parsed.path:
        raise PackageStageError("repository server must contain $arch and $repo")
    return server


def _render_build_config(repo_server: str) -> str:
    server = _validate_repo_server(repo_server)
    return (
        "[options]\n"
        "Architecture = aarch64\n"
        # Arch Linux ARM currently publishes unsigned repository databases but
        # signed package archives.  Keep package and local-file signatures
        # mandatory while allowing the database download itself to be
        # unsigned; pacman still verifies every package before installation.
        "SigLevel = Required DatabaseOptional\n"
        "LocalFileSigLevel = Required\n"
        "\n"
        "[core]\n"
        f"Server = {server}\n"
        "\n"
        "[extra]\n"
        f"Server = {server}\n"
        "\n"
        "[alarm]\n"
        f"Server = {server}\n"
    )


def _pacman_paths(target: Path) -> dict[str, Path]:
    paths = {
        "dbpath": _target_child(target, "var/lib/pacman"),
        "cachedir": _target_child(target, "var/cache/pacman/pkg"),
        "logfile": _target_child(target, "var/log/pacman.log"),
        # A tarball rootfs commonly contains the package keyring under
        # /usr/share/pacman/keyrings but has not yet created a GnuPG home.
        # That home is bootstrapped from the target keyring below.
        "gpgdir": _target_child(target, "etc/pacman.d/gnupg"),
        "hookdir": _target_child(target, "etc/pacman.d/hooks"),
        "config_source": _target_child(target, "etc/pacman.conf", required_file=True),
    }
    return paths


def _prepare_target_paths(paths: dict[str, Path]) -> None:
    for name in ("dbpath", "cachedir", "hookdir"):
        paths[name].mkdir(parents=True, exist_ok=True)
    paths["logfile"].parent.mkdir(parents=True, exist_ok=True)


def _keyring_source_files(target: Path) -> tuple[list[Path], list[Path]]:
    source = _target_child(target, "usr/share/pacman/keyrings", required_dir=True)
    keyring_files = [source / "archlinux.gpg", source / "archlinuxarm.gpg"]
    trust_files = [source / "archlinux-trusted", source / "archlinuxarm-trusted"]
    for candidate in (*keyring_files, *trust_files):
        _require_regular_file(candidate, name=f"target keyring file {candidate.name}")
    return keyring_files, trust_files


def _pacman_key_command(target: Path, destination: Path, operation: Sequence[str]) -> list[str]:
    """Run target pacman-key in nspawn, binding scratch homes when needed."""

    command = [
        "systemd-nspawn",
        "--quiet",
        "--register=no",
        "--private-users=no",
        "--directory",
        os.fspath(target),
    ]
    try:
        guest_destination = "/" + os.fspath(destination.relative_to(target))
    except ValueError:
        guest_destination = "/run/omarchy-pi-pacman-gpg"
        command.append("--bind=" + os.fspath(destination) + ":" + guest_destination)
    command.extend(
        [
            "--",
            "/usr/bin/pacman-key",
            "--gpgdir",
            guest_destination,
            "--populate-from",
            "/usr/share/pacman/keyrings",
            *operation,
        ]
    )
    return command


def _bootstrap_gpgdir(target: Path, destination: Path) -> list[str]:
    """Create a target-local pacman keyring with official local signatures."""

    keyring_files, trust_files = _keyring_source_files(target)
    pacman_key = _target_child(target, "usr/bin/pacman-key", required_file=True)
    del pacman_key  # The nspawn command deliberately invokes the target path.
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o700)
    for operation in (("--init",), ("--populate", "archlinux", "archlinuxarm")):
        command = _pacman_key_command(target, destination, operation)
        try:
            result = subprocess.run(command, check=False, capture_output=True, text=False)
        except OSError as exc:
            raise PackageStageError(f"could not run target pacman-key: {exc.strerror}") from exc
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip() if result.stderr else "no diagnostic"
            raise PackageStageError(f"target pacman-key bootstrap failed: {detail}")
    return [os.fspath(path.relative_to(target)) for path in (*keyring_files, *trust_files)]


def _prepare_gpgdir(
    target: Path,
    source: Path,
    *,
    temporary_root: Path,
    apply: bool,
) -> tuple[Path, list[str]]:
    """Select a target or scratch GPG home and run pacman-key bootstrap."""

    if source.exists():
        if source.is_symlink() or not source.is_dir():
            raise PackageStageError("target pacman GPG directory is not a real directory")
    destination = source if apply else temporary_root / "gpgdir"
    return destination, _bootstrap_gpgdir(target, destination)


def _cleanup_target_gpg_sockets(gpgdir: Path) -> list[str]:
    """Stop target GnuPG daemons and remove only their socket entries.

    ``pacman-key`` starts an agent in the target GPG home while bootstrapping
    the package trust graph.  Its Unix sockets are build-host endpoints and
    cannot be copied into an image.  Keep the public keyring, trust database,
    and generated private master key intact for this experimental step-2
    image; release-image sanitation is recorded in the package manifest.
    """

    candidate = _absolute_lexical(gpgdir)
    _reject_symlink_components(candidate)
    try:
        info = candidate.lstat()
    except FileNotFoundError as exc:
        raise PackageStageError("target pacman GPG directory disappeared before cleanup") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PackageStageError("target pacman GPG directory is not a real directory")

    # GPGCONF scopes the shutdown to this exact homedir.  Do not use a broad
    # process kill: the build host may have unrelated agents, including the
    # live Pi's own keyring when staging is performed over SSH.
    for component in ("gpg-agent", "scdaemon"):
        command = [GPGCONF_COMMAND, "--homedir", os.fspath(candidate), "--kill", component]
        try:
            result = subprocess.run(command, check=False, capture_output=True, text=False)
        except OSError as exc:
            raise PackageStageError(f"could not stop target {component}: {exc.strerror}") from exc
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip() if result.stderr else "no diagnostic"
            raise PackageStageError(f"could not stop target {component}: {detail}")

    removed: list[str] = []
    remaining_special: list[Path] = []
    for root, dirs, files in os.walk(candidate, followlinks=False):
        for name in (*dirs, *files):
            entry = Path(root) / name
            mode = entry.lstat().st_mode
            if stat.S_ISSOCK(mode):
                entry.unlink()
                removed.append(os.fspath(entry.relative_to(candidate)))
            elif not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                remaining_special.append(entry)
    if remaining_special:
        details = ", ".join(os.fspath(path.relative_to(candidate)) for path in remaining_special)
        raise PackageStageError(f"target GPG directory contains unsupported special files: {details}")
    return sorted(removed)


def _validate_fat_boot_tree(boot: Path) -> None:
    """Ensure package installation left a FAT-representable /boot tree."""

    if not boot.is_dir() or boot.is_symlink():
        raise PackageStageError("target /boot must be a real directory")
    for root, dirs, files in os.walk(boot, followlinks=False):
        for name in (*dirs, *files):
            candidate = Path(root) / name
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise PackageStageError(f"target /boot contains a symlink unsupported by FAT: {candidate}")
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise PackageStageError(f"target /boot contains an unsupported special file: {candidate}")


def _pacman_command(
    pacman: str,
    target: Path,
    paths: dict[str, Path],
    config: Path,
    operation: Sequence[str],
    *,
    nspawn: bool = False,
) -> list[str]:
    if not nspawn:
        return [
            pacman,
            "--root",
            os.fspath(target),
            "--config",
            os.fspath(config),
            "--dbpath",
            os.fspath(paths["dbpath"]),
            "--cachedir",
            os.fspath(paths["cachedir"]),
            "--logfile",
            os.fspath(paths["logfile"]),
            "--gpgdir",
            os.fspath(paths["gpgdir"]),
            "--hookdir",
            os.fspath(paths["hookdir"]),
            *operation,
        ]
    # Scriptlets and hooks need /dev/null, procfs and sysfs.  Running pacman
    # inside nspawn supplies private instances of those trees without
    # exposing host block devices to target package code.  The build config is
    # bound read-only into the container; all package paths are target-root
    # paths because nspawn makes the staged tree /.  Explicitly join the host
    # network namespace so repository DNS/downloads work without creating a
    # veth that would require target network setup.
    return [
        "systemd-nspawn",
        "--quiet",
        "--register=no",
        "--private-users=no",
        "--network-namespace-path=/proc/1/ns/net",
        "--resolv-conf=replace-host",
        "--bind-ro=" + os.fspath(config) + ":" + NSPAWN_CONFIG_PATH,
        "--directory",
        os.fspath(target),
        "--",
        pacman,
        "--root",
        "/",
        "--config",
        NSPAWN_CONFIG_PATH,
        "--dbpath",
        "/var/lib/pacman",
        "--cachedir",
        "/var/cache/pacman/pkg",
        "--logfile",
        "/var/log/pacman.log",
        "--gpgdir",
        "/etc/pacman.d/gnupg",
        "--hookdir",
        "/etc/pacman.d/hooks",
        *operation,
    ]


def _run_pacman(command: Sequence[str], *, capture: bool) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=capture,
            text=False,
        )
    except OSError as exc:
        raise PackageStageError(f"could not run pacman: {exc.strerror}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip() if capture and result.stderr else "no diagnostic"
        raise PackageStageError(f"pacman transaction failed: {detail}")
    return result


def _run_nspawn_network_preflight(target: Path, config: Path, repo_server: str) -> None:
    """Prove the nspawn network and copied resolver work before mutation."""

    hostname = urlparse(repo_server).hostname
    if not hostname:
        raise PackageStageError("repository server has no hostname for nspawn preflight")
    command = [
        "systemd-nspawn",
        "--quiet",
        "--register=no",
        "--private-users=no",
        "--network-namespace-path=/proc/1/ns/net",
        "--resolv-conf=replace-host",
        "--bind-ro=" + os.fspath(config) + ":" + NSPAWN_CONFIG_PATH,
        "--directory",
        os.fspath(target),
        "--",
        "/usr/bin/getent",
        "ahostsv4",
        hostname,
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=False)
    except OSError as exc:
        raise PackageStageError(f"could not run nspawn network preflight: {exc.strerror}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip() if result.stderr else "no diagnostic"
        raise PackageStageError(f"nspawn network preflight failed: {detail}")


def _parse_preview(stdout: bytes) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for line in stdout.decode(errors="replace").splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            continue
        name, version, repo = fields
        if not PACKAGE_NAME.fullmatch(name) or not VERSION.fullmatch(version) or not REPO_NAME.fullmatch(repo):
            continue
        records.append({"name": name, "version": version, "repository": repo})
    if not records:
        raise PackageStageError("pacman preview produced no package metadata")
    return records


def _parse_installed(stdout: bytes) -> set[str]:
    return {line.strip() for line in stdout.decode(errors="replace").splitlines() if line.strip()}


def _copy_preview_db(source: Path, destination: Path) -> None:
    if source.exists():
        if source.is_symlink() or not source.is_dir():
            raise PackageStageError("target pacman database path is not a real directory")
        shutil.copytree(source, destination, symlinks=True)
    else:
        destination.mkdir(parents=True)


def _validate_package_manifest_path(path: Path, *, target: Path) -> Path:
    candidate = _absolute_lexical(path)
    if os.path.lexists(candidate):
        raise PackageStageError(f"refusing to overwrite package manifest: {candidate}")
    _reject_symlink_components(candidate.parent)
    if not candidate.parent.is_dir():
        raise PackageStageError("package manifest parent does not exist")
    try:
        candidate.relative_to(target)
    except ValueError:
        pass
    else:
        raise PackageStageError("package manifest must be outside the target root")
    return candidate


def _write_manifest(path: Path, document: dict[str, Any], *, target: Path) -> None:
    candidate = _validate_package_manifest_path(path, target=target)
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def stage_packages(
    rootfs: Path,
    rootfs_manifest: Path,
    package_manifest: Path,
    *,
    apply: bool = False,
    repo_server: str = DEFAULT_REPO_SERVER,
    pacman_command: str = "pacman",
    require_native_aarch64: bool = True,
    require_root: bool = True,
) -> dict[str, Any]:
    """Preview or apply the bounded signed package transaction."""

    machine = platform.machine().lower()
    if require_native_aarch64 and machine != "aarch64":
        raise PackageStageError("package staging must run on a native aarch64 build host")
    if require_root and os.geteuid() != 0:
        raise PackageStageError("package staging must run as root")
    target = _require_target_root(rootfs)
    source_manifest, source_manifest_sha256 = _load_rootfs_manifest(rootfs_manifest, target)
    paths = _pacman_paths(target)
    _validate_repo_server(repo_server)
    package_manifest_path = _validate_package_manifest_path(package_manifest, target=target)

    config_text = _render_build_config(repo_server)
    target_config_sha256 = _sha256_file(paths["config_source"])
    with tempfile.TemporaryDirectory(prefix="omarchy-pi-pacman-") as temporary:
        temporary_root = Path(temporary)
        build_config = temporary_root / "pacman.conf"
        build_config.write_text(config_text, encoding="utf-8")
        # Always resolve the transaction against a complete scratch package
        # database first.  Apply mode must not preview against the live
        # target DB because generic packages still present there conflict with
        # linux-rpi until the real removal below.
        preview_paths = dict(paths)
        preview_db = temporary_root / "db"
        _copy_preview_db(paths["dbpath"], preview_db)
        preview_paths["dbpath"] = preview_db
        preview_paths["cachedir"] = temporary_root / "cache"
        preview_paths["cachedir"].mkdir()
        preview_paths["logfile"] = temporary_root / "pacman.log"
        preview_paths["hookdir"] = temporary_root / "hooks"
        preview_paths["hookdir"].mkdir()
        preview_paths["gpgdir"], keyring_sources = _prepare_gpgdir(
            target,
            paths["gpgdir"],
            temporary_root=temporary_root,
            apply=False,
        )

        targets = list(TRANSACTION_TARGETS)
        query_command = _pacman_command(
            pacman_command,
            target,
            preview_paths,
            build_config,
            ["--query", "--quiet"],
        )
        query_before_result = _run_pacman(query_command, capture=True)
        installed_before = _parse_installed(query_before_result.stdout)
        generic_to_remove = sorted(set(GENERIC_BOOT_PACKAGES) & installed_before)
        if generic_to_remove:
            # Remove only package records from the scratch database so
            # pacman can resolve linux-rpi's conflict with the generic
            # kernel/U-Boot.  The target DB remains untouched until the real
            # removal after the successful preview.
            remove_operation = ["--remove", "--dbonly", "--noconfirm", *generic_to_remove]
            remove_preview_command = _pacman_command(
                pacman_command, target, preview_paths, build_config, remove_operation
            )
            _run_pacman(remove_preview_command, capture=True)
        preview_command = _pacman_command(
            pacman_command,
            target,
            preview_paths,
            build_config,
            [
                "--sync",
                "--refresh",
                "--sysupgrade",
                "--print",
                "--print-format",
                PACMAN_PRINT_FORMAT,
                "--needed",
                "--noconfirm",
                *targets,
            ],
        )
        preview_result = _run_pacman(preview_command, capture=True)
        resolved = _parse_preview(preview_result.stdout)
        installed: list[str] | None = None
        removed_gpg_sockets: list[str] = []
        if apply:
            # Verify the exact nspawn network path used by both mutating
            # commands before removing the existing boot packages.  A DNS or
            # resolver failure must leave the target package database intact.
            _run_nspawn_network_preflight(target, build_config, repo_server)
            _prepare_target_paths(paths)
            target_paths = dict(paths)
            target_paths["gpgdir"], target_keyring_sources = _prepare_gpgdir(
                target,
                paths["gpgdir"],
                temporary_root=temporary_root,
                apply=True,
            )
            if target_keyring_sources:
                keyring_sources = target_keyring_sources
            if generic_to_remove:
                remove_command = _pacman_command(
                    pacman_command,
                    target,
                    target_paths,
                    build_config,
                    ["--remove", "--noconfirm", *generic_to_remove],
                    nspawn=True,
                )
                _run_pacman(remove_command, capture=False)
            apply_command = _pacman_command(
                pacman_command,
                target,
                target_paths,
                build_config,
                ["--sync", "--refresh", "--sysupgrade", "--needed", "--noconfirm", *targets],
                nspawn=True,
            )
            _run_pacman(apply_command, capture=False)
            # pacman-key's target-local agents are no longer needed after the
            # transaction.  Stop only those agents and remove their sockets
            # before any later image assembler copies this root tree.
            removed_gpg_sockets = _cleanup_target_gpg_sockets(target_paths["gpgdir"])
            _validate_fat_boot_tree(_target_child(target, "boot"))
            query_after_command = _pacman_command(
                pacman_command,
                target,
                target_paths,
                build_config,
                ["--query", "--quiet"],
                nspawn=True,
            )
            query_result = _run_pacman(query_after_command, capture=True)
            installed_set = _parse_installed(query_result.stdout)
            missing = sorted(set(TRANSACTION_TARGETS) - installed_set)
            generic_remaining = sorted(set(GENERIC_BOOT_PACKAGES) & installed_set)
            if missing:
                raise PackageStageError(f"transaction did not install required packages: {', '.join(missing)}")
            if generic_remaining:
                raise PackageStageError(
                    "generic boot packages remain after replacement: " + ", ".join(generic_remaining)
                )
            installed = sorted(installed_set & set(TRANSACTION_TARGETS))

        source = source_manifest.get("source", {})
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "mode": "apply" if apply else "preview",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "build_host": {"architecture": machine, "native_aarch64": machine == "aarch64"},
            "rootfs": {
                "manifest_sha256": source_manifest_sha256,
                "archive_sha256": source["archive_sha256"],
                "signer_fingerprint": source["signer_fingerprint"],
                "target_name": target.name,
                "target_pacman_conf_sha256": target_config_sha256,
            },
            "repositories": {
                "server": repo_server,
                "repositories": ["core", "extra", "alarm"],
                "architecture": "aarch64",
                "signature_policy": {
                    "packages": "Required",
                    "databases": "Optional",
                    "local_files": "Required",
                },
                "keyring": {
                    "bootstrap": "target pacman-key --init, then --populate archlinux archlinuxarm in nspawn",
                    "gpgdir": os.fspath(paths["gpgdir"]),
                    "bootstrapped_from_target_files": keyring_sources,
                    "post_transaction_cleanup": {
                        "agents_stopped": ["gpg-agent", "scdaemon"] if apply else [],
                        "removed_socket_paths": removed_gpg_sockets,
                        "special_files_verified": apply,
                    },
                    "release_sanitization": {
                        "generated_pacman_master_key_retained": apply,
                        "review_item": apply,
                        "action": (
                            "review the generated private master key and revocation certificate before distributing "
                            "the image; this experimental step retains them, and a future per-device policy may "
                            "remove them and regenerate with pacman-key --init and --populate on first boot"
                        ),
                    },
                },
            },
            "transaction": {
                "runtime_roots": list(RUNTIME_ROOTS),
                "boot_replacements": list(PI_BOOT_PACKAGES),
                "generic_boot_packages_detected": sorted(set(GENERIC_BOOT_PACKAGES) & installed_before),
                "generic_boot_packages_removed": generic_to_remove,
                "replacement_strategy": "remove detected generic boot packages, then apply a complete sysupgrade with Pi replacements and runtime roots",
                "resolved_packages": resolved,
            },
        }
        if installed is not None:
            manifest["transaction"]["installed_requested_packages"] = installed
        _write_manifest(package_manifest, manifest, target=target)
        return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rootfs", type=Path, required=True, help="previously extracted target rootfs directory")
    parser.add_argument("--rootfs-manifest", type=Path, required=True, help="verified rootfs provenance manifest")
    parser.add_argument("--package-manifest", type=Path, required=True, help="new package transaction record")
    parser.add_argument("--apply", action="store_true", help="apply the transaction; default is a scratch preview")
    parser.add_argument("--repo-server", default=DEFAULT_REPO_SERVER, help="official Arch Linux ARM repository URL template")
    parser.add_argument("--pacman", default="pacman", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = stage_packages(
            args.rootfs,
            args.rootfs_manifest,
            args.package_manifest,
            apply=args.apply,
            repo_server=args.repo_server,
            pacman_command=args.pacman,
        )
    except (PackageStageError, OSError, subprocess.CalledProcessError) as exc:
        print(f"stage-arm-packages: error: {exc}", file=sys.stderr)
        return 2
    print(f"{manifest['mode']} package transaction recorded at {args.package_manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
