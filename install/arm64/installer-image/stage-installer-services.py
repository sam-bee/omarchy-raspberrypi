#!/usr/bin/python3
"""Stage the reviewed USB installer services into an isolated target root.

This is an offline filesystem operation.  It never invokes a live service
manager, installs packages, writes boot media, or copies a filled-in settings
file.  The normal command requires root so every installed file and symlink is
owned by root; tests may opt out of that check and use the current UID.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
from typing import Iterable, Sequence


BUILDER_MARKER = Path("usr/lib/omarchy-pi/installer-image.marker")
BUILDER_MARKER_CONTENT = b"omarchy-pi-installer-image-v1\n"
EXAMPLE_SETTINGS = "installer-settings.example.toml"
NETWORKD_PRESET = Path("etc/systemd/system-preset/00-omarchy-installer-networkd.preset")
NETWORKD_PRESET_CONTENT = b"disable systemd-networkd*\n"
EXPECTED_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
ELF_MACHINE_AARCH64 = 183
PT_INTERP = 3
# The target RDP service runs this public validator after installation.  Keep
# its source tied to the reviewed session runtime instead of requiring a
# separately assembled native archive to provide a possibly stale copy.
RUNTIME_VALIDATOR_SOURCE = Path(__file__).resolve().parents[1] / "session/systemd/verify-hypr-rdp-runtime.py"
RUNTIME_VALIDATOR_DESTINATION = "verify-hypr-rdp-runtime.py"

LIBEXEC_FILES = {
    "settings.py": 0o644,
    "disk_install.py": 0o644,
    "installer_job.py": 0o644,
    "installed_target.py": 0o644,
    "desktop_payload.py": 0o644,
    "configure-installer-boot.py": 0o644,
    "assemble-image.py": 0o644,
    "installer-control": 0o755,
    "provision-access.py": 0o755,
    "provision-network.py": 0o755,
    "provision-rdp.py": 0o755,
    "verify-installer-rdp-runtime.py": 0o755,
    "launch-installer-session.py": 0o755,
    "start-installer-session.sh": 0o755,
    "start-installer-desktop.sh": 0o755,
}
SHARE_FILES = {"installer-hyprland.conf": 0o644}
SYSTEM_UNITS = {
    "omarchy-pi-install.service": 0o644,
    "omarchy-pi-provision-access.service": 0o644,
    "omarchy-pi-provision-network.service": 0o644,
    "omarchy-pi-provision-rdp.service": 0o644,
    "omarchy-installer-launch.service": 0o644,
    "omarchy-installer-session@.service": 0o644,
}
USER_UNITS = {"omarchy-installer-rdp.service": 0o644}
BOOT_ENABLED_UNITS = (
    "omarchy-pi-provision-access.service",
    "omarchy-pi-provision-network.service",
    "omarchy-pi-provision-rdp.service",
    "omarchy-installer-launch.service",
)
# These are the public directory components traversed by the staged payload.
# Keep the list explicit: the build runs under umask 077, and using mkdir's
# parents=True alone would leave newly-created ancestors inaccessible to the
# non-root installer session. Private directories outside this payload remain
# untouched.
PUBLIC_PAYLOAD_DIRECTORIES = (
    "usr",
    "usr/local",
    "usr/local/bin",
    "usr/local/libexec",
    "usr/local/libexec/omarchy-pi",
    "usr/local/share",
    "usr/local/share/omarchy-pi",
    "usr/lib",
    "usr/lib/omarchy-pi",
    "usr/bin",
    "usr/share",
    "usr/share/omarchy-pi",
    "etc",
    "etc/systemd",
    "etc/systemd/system",
    "etc/systemd/system-preset",
    "etc/systemd/user",
    "etc/systemd/system/multi-user.target.wants",
    "etc/systemd/system/network-pre.target.requires",
    "etc/systemd/system/sshd.service.requires",
    "etc/systemd/system/NetworkManager.service.requires",
    "etc/systemd/user/graphical-session.target.wants",
)


class ServiceStageError(RuntimeError):
    """Raised when the isolated target cannot be staged safely."""


@dataclass(frozen=True)
class StageResult:
    target_root: Path
    binary_sha256: str
    dynamic_dependency_check: str
    installed_files: tuple[str, ...]
    enabled_links: tuple[str, ...]
    installer_provenance: dict | None = None


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
            raise ServiceStageError(f"refusing symlink path component: {current}")


def _target_root(path: Path) -> Path:
    candidate = _absolute(path)
    _reject_symlink_components(candidate)
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise ServiceStageError("target root is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ServiceStageError("target root must be a real directory")
    resolved = candidate.resolve(strict=True)
    if resolved == Path("/"):
        raise ServiceStageError("refusing the host root")
    for protected in (Path("/dev"), Path("/proc"), Path("/sys"), Path("/run"), Path("/boot")):
        try:
            resolved.relative_to(protected)
        except ValueError:
            continue
        raise ServiceStageError(f"refusing target under {protected}")
    return resolved


def _source_file(source_root: Path, name: str) -> Path:
    path = source_root / name
    _reject_symlink_components(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ServiceStageError(f"source file is missing: {name}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ServiceStageError(f"source file is not a regular file: {name}")
    return path


def _source_path(path: Path, description: str) -> Path:
    """Validate a declared source outside the installer-image directory."""

    _reject_symlink_components(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ServiceStageError(f"source file is missing: {description}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ServiceStageError(f"source file is not a regular file: {description}")
    return path


def _safe_target_path(target: Path, relative: str) -> Path:
    path = target / relative
    try:
        path.relative_to(target)
    except ValueError as exc:
        raise ServiceStageError(f"target path escapes root: {relative}") from exc
    _reject_symlink_components(path)
    return path


def _ensure_directory(path: Path, *, owner_uid: int, owner_gid: int, mode: int = 0o755) -> None:
    _reject_symlink_components(path)
    if os.path.lexists(path):
        try:
            info = path.lstat()
        except OSError as exc:
            raise ServiceStageError(f"cannot inspect target directory: {path}") from exc
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ServiceStageError(f"target path is not a real directory: {path}")
        if info.st_uid != owner_uid or info.st_mode & 0o022:
            raise ServiceStageError(f"target directory has unsafe owner or mode: {path}")
        if stat.S_IMODE(info.st_mode) != mode:
            try:
                os.chmod(path, mode)
            except OSError as exc:
                raise ServiceStageError(f"cannot normalize target directory mode: {path}") from exc
        return
    try:
        path.mkdir(mode=mode, parents=True, exist_ok=False)
        os.chown(path, owner_uid, owner_gid)
        os.chmod(path, mode)
    except OSError as exc:
        raise ServiceStageError(f"cannot create target directory: {path}") from exc


def _file_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ServiceStageError(f"cannot read source file: {path}") from exc


def _install_payload(
    payload: bytes,
    destination: Path,
    *,
    mode: int,
    owner_uid: int,
    owner_gid: int,
    overwrite_identical: bool = True,
) -> bool:
    """Install bytes, accepting an already identical destination only."""

    if os.path.lexists(destination):
        try:
            info = destination.lstat()
        except OSError as exc:
            raise ServiceStageError(f"cannot inspect target file: {destination}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ServiceStageError(f"target file is not a regular file: {destination}")
        if not overwrite_identical or info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) != mode or _file_bytes(destination) != payload:
            raise ServiceStageError(f"refusing to replace a different target file: {destination}")
        return False
    parent = destination.parent
    _ensure_directory(parent, owner_uid=owner_uid, owner_gid=owner_gid)
    temporary = parent / f".{destination.name}.stage-tmp"
    if os.path.lexists(temporary):
        raise ServiceStageError(f"staging temporary path already exists: {temporary}")
    descriptor = -1
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        os.fchown(descriptor, owner_uid, owner_gid)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except OSError as exc:
        raise ServiceStageError(f"cannot install target file: {destination}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return True


def _install_file(
    source: Path,
    destination: Path,
    *,
    mode: int,
    owner_uid: int,
    owner_gid: int,
    overwrite_identical: bool = True,
) -> bool:
    """Install one file, accepting an already identical destination only."""

    return _install_payload(
        _file_bytes(source),
        destination,
        mode=mode,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        overwrite_identical=overwrite_identical,
    )


def _validate_binary(path: Path, expected_sha256: str) -> tuple[str, bytes]:
    candidate = _absolute(path)
    _reject_symlink_components(candidate)
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise ServiceStageError("hypr-rdp binary is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or not info.st_size:
        raise ServiceStageError("hypr-rdp binary must be a nonempty regular file")
    if not EXPECTED_DIGEST.fullmatch(expected_sha256):
        raise ServiceStageError("expected binary SHA-256 must be 64 lowercase hexadecimal characters")
    payload = _file_bytes(candidate)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected_sha256:
        raise ServiceStageError("hypr-rdp binary SHA-256 does not match the expected digest")
    if len(payload) < 20 or payload[:4] != b"\x7fELF" or payload[4] != 2:
        raise ServiceStageError("hypr-rdp binary is not a 64-bit ELF executable")
    endian = "<" if payload[5] == 1 else ">" if payload[5] == 2 else ""
    if not endian or struct.unpack_from(f"{endian}H", payload, 18)[0] != ELF_MACHINE_AARCH64:
        raise ServiceStageError("hypr-rdp binary is not an aarch64 executable")
    return digest, payload


def _elf_interpreter(payload: bytes) -> str | None:
    endian = "<" if payload[5] == 1 else ">"
    try:
        phoff = struct.unpack_from(f"{endian}Q", payload, 32)[0]
        phentsize = struct.unpack_from(f"{endian}H", payload, 54)[0]
        phnum = struct.unpack_from(f"{endian}H", payload, 56)[0]
    except struct.error:
        return None
    for index in range(phnum):
        start = phoff + index * phentsize
        try:
            p_type = struct.unpack_from(f"{endian}I", payload, start)[0]
            if p_type != PT_INTERP:
                continue
            p_offset = struct.unpack_from(f"{endian}Q", payload, start + 8)[0]
            p_filesz = struct.unpack_from(f"{endian}Q", payload, start + 32)[0]
            raw = payload[p_offset : p_offset + p_filesz]
        except struct.error:
            return None
        if raw and raw[-1:] == b"\0":
            raw = raw[:-1]
        try:
            value = raw.decode("ascii")
        except UnicodeDecodeError:
            return None
        return value if value.startswith("/") else None
    return None


def _target_regular_or_symlink(root: Path, absolute: str) -> bool:
    try:
        path = root / absolute.lstrip("/")
        path.relative_to(root)
        info = path.lstat()
    except (OSError, ServiceStageError):
        return False
    if stat.S_ISREG(info.st_mode):
        return True
    if not stat.S_ISLNK(info.st_mode):
        return False
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _dynamic_dependencies(root: Path, payload: bytes, binary: Path) -> str:
    interpreter = _elf_interpreter(payload)
    if interpreter and not _target_regular_or_symlink(root, interpreter):
        raise ServiceStageError(f"target root is missing ELF interpreter {interpreter}")
    readelf = shutil.which("readelf")
    if readelf is None:
        return "interpreter-checked; readelf-unavailable"
    try:
        result = subprocess.run([readelf, "-d", "--", os.fspath(binary)], check=False, capture_output=True, text=True)
    except OSError:
        return "interpreter-checked; readelf-unavailable"
    if result.returncode != 0:
        return "interpreter-checked; readelf-unavailable"
    needed = re.findall(r"Shared library: \[([^\]]+)\]", result.stdout)
    missing: list[str] = []
    for name in needed:
        found = False
        for directory in ("lib", "usr/lib", "lib/aarch64-linux-gnu", "usr/lib/aarch64-linux-gnu"):
            if _target_regular_or_symlink(root, "/" + directory + "/" + name):
                found = True
                break
        if not found:
            missing.append(name)
    if missing:
        raise ServiceStageError("target root is missing dynamic libraries: " + ", ".join(sorted(missing)))
    return f"checked {len(needed)} NEEDED entries"


def _unit_source_names() -> Iterable[tuple[str, Path, int, str]]:
    root = Path(__file__).resolve().parent
    for name, mode in LIBEXEC_FILES.items():
        yield name, _source_file(root, name), mode, "libexec"
    yield (
        RUNTIME_VALIDATOR_DESTINATION,
        _source_path(RUNTIME_VALIDATOR_SOURCE, "session/systemd/verify-hypr-rdp-runtime.py"),
        0o755,
        "libexec",
    )
    yield "omarchy-pi-install", _source_file(root, "omarchy-pi-install"), 0o755, "bin"
    for name, mode in SHARE_FILES.items():
        yield name, _source_file(root, name), mode, "share"
    for name, mode in SYSTEM_UNITS.items():
        yield name, _source_file(root, name), mode, "system"
    for name, mode in USER_UNITS.items():
        yield name, _source_file(root, name), mode, "user"


def _symlink(path: Path, target: str, *, owner_uid: int, owner_gid: int) -> bool:
    if os.path.lexists(path):
        try:
            info = path.lstat()
        except OSError as exc:
            raise ServiceStageError(f"cannot inspect unit link: {path}") from exc
        if not stat.S_ISLNK(info.st_mode) or os.readlink(path) != target:
            raise ServiceStageError(f"unit link differs from the reviewed target: {path}")
        if info.st_uid != owner_uid:
            raise ServiceStageError(f"unit link has the wrong owner: {path}")
        return False
    _ensure_directory(path.parent, owner_uid=owner_uid, owner_gid=owner_gid)
    try:
        os.symlink(target, path)
        os.lchown(path, owner_uid, owner_gid)
    except OSError as exc:
        raise ServiceStageError(f"cannot create unit link: {path}") from exc
    return True


def _disable_networkd_link(path: Path, *, owner_uid: int) -> bool:
    if not os.path.lexists(path):
        return False
    try:
        info = path.lstat()
        target = os.readlink(path) if stat.S_ISLNK(info.st_mode) else ""
    except OSError as exc:
        raise ServiceStageError(f"cannot inspect networkd enablement: {path}") from exc
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != owner_uid or Path(target).name not in {
        "systemd-networkd.service",
        "systemd-networkd.socket",
        "systemd-networkd-wait-online.service",
        "systemd-networkd-resolve-hook.socket",
        "systemd-networkd-varlink-metrics.socket",
        "systemd-networkd-varlink.socket",
    }:
        raise ServiceStageError(f"refusing to remove an unexpected networkd path: {path}")
    try:
        path.unlink()
    except OSError as exc:
        raise ServiceStageError(f"cannot disable networkd path: {path}") from exc
    return True


def _assert_no_networkd_enablement(target: Path) -> None:
    """Fail closed if any remaining wants/requires link would start networkd."""

    for systemd in (target / "etc/systemd/system", target / "usr/lib/systemd/system"):
        if not systemd.is_dir():
            continue
        for root, _directories, files in os.walk(systemd, followlinks=False):
            parent = Path(root)
            if not (parent.name.endswith(".wants") or parent.name.endswith(".requires")):
                continue
            for name in files:
                path = parent / name
                if not path.is_symlink():
                    continue
                try:
                    destination = os.readlink(path)
                except OSError as exc:
                    raise ServiceStageError(f"cannot inspect remaining enablement: {path}") from exc
                if Path(destination).name.startswith("systemd-networkd"):
                    raise ServiceStageError(f"networkd enablement remains after staging: {path}")


def _enablement_links(target: Path, *, owner_uid: int, owner_gid: int) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    wants = target / "etc/systemd/system/multi-user.target.wants"
    for unit in ("NetworkManager.service", "sshd.service"):
        links.append((os.fspath(wants / unit), f"/usr/lib/systemd/system/{unit}"))
    for unit in BOOT_ENABLED_UNITS:
        links.append((os.fspath(wants / unit), f"/etc/systemd/system/{unit}"))
    for unit, requirements in {
        "omarchy-pi-provision-access.service": ("network-pre.target", "sshd.service"),
        "omarchy-pi-provision-network.service": ("network-pre.target", "NetworkManager.service", "sshd.service"),
    }.items():
        for required_by in requirements:
            links.append(
                (
                    os.fspath(target / "etc/systemd/system" / f"{required_by}.requires" / unit),
                    f"/etc/systemd/system/{unit}",
                )
            )
    return links


def stage_services(
    target_root: Path,
    binary: Path,
    expected_sha256: str,
    *,
    require_root: bool = True,
    source_revision: str | None = None,
) -> StageResult:
    if source_revision is not None and not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        raise ServiceStageError("installer source revision must be a full Git commit hash")
    if require_root and os.geteuid() != 0:
        raise ServiceStageError("service staging must run as root")
    target = _target_root(target_root)
    owner_uid = 0 if require_root else os.getuid()
    owner_gid = 0 if require_root else os.getgid()
    source_root = Path(__file__).resolve().parent
    sources = list(_unit_source_names())
    example_source = _source_file(source_root, EXAMPLE_SETTINGS)
    digest, payload = _validate_binary(binary, expected_sha256)
    dynamic_check = _dynamic_dependencies(target, payload, _absolute(binary))

    private_settings = target / "boot/installer-settings.toml"
    if os.path.lexists(private_settings):
        raise ServiceStageError("target contains private installer-settings.toml")

    for relative in PUBLIC_PAYLOAD_DIRECTORIES:
        _ensure_directory(target / relative, owner_uid=owner_uid, owner_gid=owner_gid)
    _ensure_directory(target / "boot", owner_uid=owner_uid, owner_gid=owner_gid)

    installed: list[str] = []
    provenance_files: dict[str, str] = {}
    for name, source, mode, area in sources:
        if area == "libexec":
            destination = target / "usr/local/libexec/omarchy-pi" / name
        elif area == "bin":
            destination = target / "usr/local/bin" / name
        elif area == "share":
            destination = target / "usr/local/share/omarchy-pi" / name
        elif area == "system":
            destination = target / "etc/systemd/system" / name
        else:
            destination = target / "etc/systemd/user" / name
        provenance_files[os.fspath(destination.relative_to(target))] = hashlib.sha256(_file_bytes(source)).hexdigest()
        if _install_file(source, destination, mode=mode, owner_uid=owner_uid, owner_gid=owner_gid):
            installed.append(os.fspath(destination.relative_to(target)))

    marker = target / BUILDER_MARKER
    marker_source = target / "usr/lib/omarchy-pi/.installer-image-marker.stage"
    if os.path.lexists(marker_source):
        raise ServiceStageError(f"staging temporary path already exists: {marker_source}")
    marker_source.write_bytes(BUILDER_MARKER_CONTENT)
    try:
        if _install_file(marker_source, marker, mode=0o644, owner_uid=owner_uid, owner_gid=owner_gid):
            installed.append(os.fspath(marker.relative_to(target)))
    finally:
        marker_source.unlink(missing_ok=True)

    example_destination = target / "boot" / EXAMPLE_SETTINGS
    if _install_file(example_source, example_destination, mode=0o644, owner_uid=owner_uid, owner_gid=owner_gid):
        installed.append(os.fspath(example_destination.relative_to(target)))

    binary_destination = target / "usr/bin/hypr-rdp"
    binary_source = _absolute(binary)
    if _install_file(binary_source, binary_destination, mode=0o755, owner_uid=owner_uid, owner_gid=owner_gid):
        installed.append(os.fspath(binary_destination.relative_to(target)))
    digest_source = target / "usr/share/omarchy-pi/.hypr-rdp.sha256.stage"
    if os.path.lexists(digest_source):
        raise ServiceStageError(f"staging temporary path already exists: {digest_source}")
    digest_source.write_text(digest + "\n", encoding="ascii")
    try:
        if _install_file(digest_source, target / "usr/share/omarchy-pi/hypr-rdp.sha256", mode=0o644, owner_uid=owner_uid, owner_gid=owner_gid):
            installed.append("usr/share/omarchy-pi/hypr-rdp.sha256")
    finally:
        digest_source.unlink(missing_ok=True)

    if _install_payload(
        NETWORKD_PRESET_CONTENT,
        target / NETWORKD_PRESET,
        mode=0o644,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
    ):
        installed.append(os.fspath(NETWORKD_PRESET))

    for relative in (
        "etc/systemd/system/multi-user.target.wants/systemd-networkd.service",
        "etc/systemd/system/sockets.target.wants/systemd-networkd.socket",
        "etc/systemd/system/network-online.target.wants/systemd-networkd-wait-online.service",
        "etc/systemd/system/network.target.wants/systemd-networkd.service",
        "etc/systemd/system/dbus-org.freedesktop.network1.service",
        "etc/systemd/system/sockets.target.wants/systemd-networkd-resolve-hook.socket",
        "etc/systemd/system/sockets.target.wants/systemd-networkd-varlink-metrics.socket",
        "etc/systemd/system/sockets.target.wants/systemd-networkd-varlink.socket",
    ):
        _disable_networkd_link(target / relative, owner_uid=owner_uid)
    _assert_no_networkd_enablement(target)

    enabled: list[str] = []
    for link, destination in _enablement_links(target, owner_uid=owner_uid, owner_gid=owner_gid):
        destination_path = target / destination.lstrip("/")
        if not destination_path.is_file() and not destination_path.is_symlink():
            raise ServiceStageError(f"enabled unit is missing from target: {destination}")
        link_path = Path(link)
        if _symlink(link_path, destination, owner_uid=owner_uid, owner_gid=owner_gid):
            enabled.append(os.fspath(link_path.relative_to(target)))

    user_wants = target / "etc/systemd/user/graphical-session.target.wants/omarchy-installer-rdp.service"
    if _symlink(user_wants, "../omarchy-installer-rdp.service", owner_uid=owner_uid, owner_gid=owner_gid):
        enabled.append(os.fspath(user_wants.relative_to(target)))

    provenance = {
        "schema_version": 1,
        "source_revision": source_revision,
        "runtime_sha256": hashlib.sha256(json.dumps(provenance_files, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "files": provenance_files,
    }
    provenance_path = target / "usr/lib/omarchy-pi/installer-provenance.json"
    if _install_payload((json.dumps(provenance, sort_keys=True, indent=2) + "\n").encode(), provenance_path,
                        mode=0o644, owner_uid=owner_uid, owner_gid=owner_gid):
        installed.append(os.fspath(provenance_path.relative_to(target)))
    return StageResult(target, digest, dynamic_check, tuple(installed), tuple(enabled), provenance)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rootfs", "--target-root", dest="target_root", type=Path, required=True)
    parser.add_argument("--hypr-rdp", dest="binary", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--installer-source-revision", help="full commit hash; runtime hashes are always recorded")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = stage_services(args.target_root, args.binary, args.expected_sha256, source_revision=args.installer_source_revision)
    except (OSError, ServiceStageError) as exc:
        print(f"stage-installer-services: error: {exc}", file=sys.stderr)
        return 2
    print(
        "staged installer services: "
        f"target={result.target_root} sha256={result.binary_sha256} "
        f"dynamic={result.dynamic_dependency_check} files={len(result.installed_files)} links={len(result.enabled_links)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
