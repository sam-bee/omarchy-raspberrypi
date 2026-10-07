#!/usr/bin/python3
"""Build and validate the two Omarchy runtime packages for a Pi image.

The builder intentionally has no dependency on an Arch build host.  It emits
the native pacman archive format directly (a tar archive compressed with
zstd), which is sufficient for a package containing architecture-independent
shell, Lua, QML, and configuration data.  The package metadata still names
``aarch64`` so that a target pacman transaction cannot silently install these
archives on another architecture.

The source is a clean, pinned Git checkout.  Only the source trees listed by
the package map are copied into the staging roots.  In particular, the Pi
package pair does not own bootloader, kernel, network, authentication, or
initramfs configuration.  Those boundaries belong to the installer and the
existing Pi substrate.

Build mode::

    build-runtime-packages.py build --source-checkout CHECKOUT --output BUNDLE

Validation mode::

    build-runtime-packages.py --check BUNDLE

``--source-sha256`` is the SHA-256 of the deterministic ``git archive``
stream.  When omitted it is calculated and recorded.  A caller preparing a
release can pass the expected value to make provenance drift fail closed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Iterable, Iterator, Sequence


HERE = Path(__file__).resolve().parent
ARCHITECTURE = "aarch64"
PACKAGE_NAMES = ("omarchy-settings", "omarchy")
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
FULL_REVISION = re.compile(r"[0-9a-f]{40}\Z")
PACKAGE_VERSION = re.compile(r"[A-Za-z0-9@+._:~-]+\Z")
CHANNEL = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")


class RuntimePackageError(RuntimeError):
    """Raised when a runtime package cannot be built safely."""


@dataclass(frozen=True)
class PackageSpec:
    name: str
    description: str
    depends: tuple[str, ...] = ()


PACKAGE_SPECS = {
    "omarchy-settings": PackageSpec(
        "omarchy-settings",
        "Omarchy defaults and static settings for Raspberry Pi",
    ),
    "omarchy": PackageSpec(
        "omarchy",
        "Omarchy runtime commands and desktop source for Raspberry Pi",
        ("omarchy-settings={version}",),
    ),
}

# These are deliberately allow-listed rather than copied from ``default/``
# wholesale.  limine, pacman, snapper, Plymouth, SDDM, udev and system-wide
# hardware files can change the protected Pi substrate during installation.
SAFE_DEFAULT_ROOTS = (
    "agents",
    "alacritty",
    "applications",
    "audio",
    "bash",
    "chromium",
    "environment.d",
    "firefox",
    "fontconfig",
    "fonts",
    "foot",
    "ghostty",
    "gpg",
    "hypr",
    "nautilus-python",
    "omarchy",
    "themed",
    "tensaku",
    "uwsm",
    "v4l2-relayd",
    "voxtype",
    "wayland-sessions",
    "wireplumber",
    "xdg-terminal-exec",
)

# Files copied from this repository's conventional package layout into the
# settings package.  The two /etc entries are static defaults and have no
# authority over the Pi's network, boot, or authentication path.
SAFE_ETC_FILES = {
    "etc/fastfetch/config.jsonc": "etc/fastfetch/config.jsonc",
    "etc/xdg/kitty/kitty.conf": "etc/xdg/kitty/kitty.conf",
    "etc/profile.d/omarchy.sh": "etc/profile.d/omarchy.sh",
}

# The Pi session's helpers are package-owned because they are part of the
# packaged runtime contract.  They do not replace the target's network,
# bootloader, PAM, or account configuration.  The lock-password PAM file is
# intentionally absent from this map.
SAFE_PI_ASSETS = {
    "install/arm64/session/systemd/omarchy-pi-uwsm-session@.service": "usr/lib/systemd/system/omarchy-pi-uwsm-session@.service",
    "install/arm64/session/systemd/omarchy-pi-hypr-rdp.service": "usr/lib/systemd/user/omarchy-pi-hypr-rdp.service",
    "install/arm64/session/systemd/start-uwsm-session.sh": "usr/libexec/omarchy-pi/start-uwsm-session.sh",
    "install/arm64/session/systemd/verify-hypr-rdp-runtime.py": "usr/libexec/omarchy-pi/verify-hypr-rdp-runtime.py",
    "install/arm64/session/ensure-headless-output.sh": "usr/libexec/omarchy-pi/ensure-headless-output.sh",
    "install/arm64/session/90-omarchy-pi": "etc/skel/.config/uwsm/env.d/90-omarchy-pi",
    # These are Pi session defaults rather than upstream defaults.  Keep them
    # in the package-owned skeleton so user creation after a package install
    # has the same portal, terminal, and Chromium settings as the installer
    # provisioner.
    "install/arm64/session/chromium-flags.conf": "etc/skel/.config/chromium-flags.conf",
    "install/arm64/session/portals.conf": "etc/skel/.config/xdg-desktop-portal/portals.conf",
    "install/arm64/session/xdg-terminals.list": "etc/skel/.config/xdg-terminals.list",
}

ICON_DESTINATION_SIZES = ("48x48", "256x256", "scalable")

# Packaged provisioning writes this exact environment into the selected
# user's home after /etc/skel has been applied.  Keep the package-owned skel
# copy byte-identical so a user created directly by useradd and a user created
# through the Pi provisioner receive the same packaged defaults.  The source
# 90-omarchy-pi template remains conditional for legacy source releases.
PACKAGED_RUNTIME_ENV = """# Omarchy Pi target environment. The Omarchy runtime is package-owned.
export OMARCHY_PATH=\"/usr/share/omarchy\"
export OMARCHY_PI_RUNTIME_MODE=packaged
case \":${PATH:-}:\" in
  *\":$HOME/.local/share/mise/shims:\"*) ;;
  *) export PATH=\"${PATH:+$PATH:}$HOME/.local/share/mise/shims\" ;;
esac
case \":${PATH:-}:\" in
  *\":$HOME/.local/bin:\"*) ;;
  *) export PATH=\"${PATH:+$PATH:}$HOME/.local/bin\" ;;
esac
export TERMINAL=xdg-terminal-exec
"""

FORBIDDEN_PATH_PARTS = (
    "boot/",
    "default/limine/",
    "default/pacman/",
    "default/plymouth/",
    "default/sddm/",
    "default/snapper/",
    "default/systemd/system-sleep/",
    "default/udev/",
    "etc/NetworkManager/",
    "etc/cryptsetup-keys.d/",
    "etc/crypttab",
    "etc/fstab",
    "etc/limine-entry-tool.d/",
    "etc/mkinitcpio",
    "etc/modprobe.d/",
    "etc/pam.d/",
    "etc/security/",
    "etc/ssh/",
    "etc/sudoers",
    "etc/systemd/",
    "etc/udev/",
    "default/libalpm/hooks/",
    "install/arm64/session/omarchy-lock-password",
    "credentials/",
)


def _absolute(path: Path) -> Path:
    if path.is_absolute():
        return Path(os.path.normpath(os.fspath(path)))
    return Path(os.path.normpath(os.path.join(os.getcwd(), os.fspath(path))))


def _reject_symlink_components(path: Path, *, include_leaf: bool = True) -> None:
    candidate = _absolute(path)
    current = Path(candidate.anchor)
    components = candidate.parts[1:]
    if not include_leaf:
        components = components[:-1]
    for component in components:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise RuntimePackageError(f"refusing symlink path component: {current}")


def _real_directory(path: Path, *, name: str) -> Path:
    candidate = _absolute(path)
    _reject_symlink_components(candidate)
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise RuntimePackageError(f"{name} does not exist: {candidate}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimePackageError(f"{name} must be a real directory: {candidate}")
    return candidate


def _regular_file(path: Path, *, name: str) -> Path:
    candidate = _absolute(path)
    _reject_symlink_components(candidate)
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise RuntimePackageError(f"{name} does not exist: {candidate}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size == 0:
        raise RuntimePackageError(f"{name} must be a nonempty regular file: {candidate}")
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(command: Sequence[str], *, check: bool = True, input_data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(command, check=check, input=input_data, capture_output=True)
    except OSError as exc:
        raise RuntimePackageError(f"could not run {command[0]}: {exc.strerror}") from exc
    if check and result.returncode:
        detail = result.stderr.decode(errors="replace").strip() or "no diagnostic"
        raise RuntimePackageError(f"{' '.join(command)} failed ({result.returncode}): {detail}")
    return result


def _validate_sha256(value: str, *, name: str) -> str:
    if not HEX_SHA256.fullmatch(value):
        raise RuntimePackageError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _validate_revision(value: str) -> str:
    if not FULL_REVISION.fullmatch(value):
        raise RuntimePackageError("source revision must be a full 40-character lowercase commit")
    return value


def _validate_version(value: str) -> str:
    if not PACKAGE_VERSION.fullmatch(value) or value.startswith(("-", ".")):
        raise RuntimePackageError(f"invalid package version: {value!r}")
    # A pacman package version always includes pkgrel.  The source's version
    # file is a pkgver, so make the default release explicit while accepting a
    # caller-supplied complete version such as 4.0.0.alpha-2.
    if not re.search(r"-[0-9]+\Z", value):
        value += "-1"
    return value


def _validate_channel(value: str) -> str:
    if not CHANNEL.fullmatch(value):
        raise RuntimePackageError(f"invalid package channel: {value!r}")
    return value


def _validate_source(source: Path, revision: str | None) -> tuple[str, str]:
    source = _real_directory(source, name="source checkout")
    top = _run(["git", "-C", os.fspath(source), "rev-parse", "--show-toplevel"]).stdout.decode().strip()
    if Path(top).resolve() != source.resolve():
        raise RuntimePackageError("source must be the top-level Git checkout")
    status = _run(["git", "-C", os.fspath(source), "status", "--porcelain", "--untracked-files=all"]).stdout
    if status.strip():
        raise RuntimePackageError("source checkout must be clean")
    actual = _run(["git", "-C", os.fspath(source), "rev-parse", "--verify", "HEAD^{commit}"]).stdout.decode().strip()
    _validate_revision(actual)
    if revision is not None:
        revision = _validate_revision(revision)
        if revision != actual:
            raise RuntimePackageError(f"source revision differs from checkout HEAD: {revision} != {actual}")
    for path in _run(["git", "-C", os.fspath(source), "ls-files", "-z"]).stdout.decode(errors="surrogateescape").split("\0"):
        if path and (path == ".env" or path.startswith("credentials/") or "/.env" in path):
            raise RuntimePackageError(f"source checkout contains credential-like path: {path}")
    return source.as_posix(), actual


def _source_revision_count(source: Path, revision: str) -> int:
    value = _run(["git", "-C", os.fspath(source), "rev-list", "--count", revision]).stdout.decode().strip()
    if not value.isdigit() or int(value) < 1:
        raise RuntimePackageError("source revision has no positive Git commit count")
    return int(value)


def _archive_source(source: Path, revision: str, destination: Path) -> str:
    result = _run(["git", "-C", os.fspath(source), "archive", "--format=tar", revision])
    destination.write_bytes(result.stdout)
    return _sha256(destination)


def _extract_archive(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:") as stream:
        for member in stream.getmembers():
            name = Path(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise RuntimePackageError(f"source archive contains unsafe name: {member.name}")
            target = destination / name
            if member.issym() or member.islnk():
                # A package source may contain a symlink, but it must remain
                # inside the extracted source tree.
                link = Path(member.linkname)
                link_target = Path(os.path.normpath(os.fspath(target.parent / link)))
                try:
                    link_target.relative_to(destination)
                except ValueError:
                    raise RuntimePackageError(f"source archive contains unsafe link: {member.name}")
            stream.extract(member, destination, set_attrs=False)
            if not (member.issym() or member.islnk()):
                target.chmod(member.mode & 0o7777)


def _path_allowed(relative: str, *, package: str) -> bool:
    if any(relative == part.rstrip("/") or relative.startswith(part) for part in FORBIDDEN_PATH_PARTS):
        return False
    if package == "omarchy-settings":
        if relative.startswith("default/"):
            if relative.startswith("default/systemd/user/"):
                return True
            return relative.split("/", 2)[1] in SAFE_DEFAULT_ROOTS
        return relative.startswith("config/") or relative.startswith("applications/") or relative in SAFE_ETC_FILES
    return relative.startswith(("bin/", "shell/", "install/", "migrations/", "themes/")) or relative == "version"


def _iter_files(root: Path, relative_root: str) -> Iterator[tuple[str, Path]]:
    base = root / relative_root
    if not base.exists() and not base.is_symlink():
        return
    if base.is_file() or base.is_symlink():
        yield relative_root, base
        return
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_dir() and not path.is_symlink():
            continue
        yield rel, path


def _copy_entry(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        target = os.readlink(source)
        if destination.exists() or destination.is_symlink():
            destination.unlink()
        destination.symlink_to(target)
        return
    shutil.copy2(source, destination)
    mode = stat.S_IMODE(source.lstat().st_mode)
    # A checkout can inherit a cooperative umask (the validated source tree
    # currently has several 0664 files).  Package archives must not publish
    # group/world-writable files merely because of the builder's checkout
    # permissions; retain executable and special bits while clearing write
    # access for non-owners.
    destination.chmod((mode & 0o7777) & ~0o022)


def _copy_packaged_pi_asset(source: Path, destination: Path) -> None:
    """Copy a Pi asset, retargeting packaged units to /usr/libexec.

    The source templates are also consumed by the legacy provisioner, which
    deliberately stages them under /usr/local/libexec and /etc/systemd.  Keep
    those templates unchanged and rewrite only the copies owned by the
    packaged settings archive.
    """

    if source.suffix == ".service":
        content = source.read_text(encoding="utf-8")
        packaged = content.replace(
            "/usr/local/libexec/omarchy-pi/",
            "/usr/libexec/omarchy-pi/",
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(packaged, encoding="utf-8")
        destination.chmod(stat.S_IMODE(source.lstat().st_mode) & ~0o022)
        return
    if destination.as_posix().endswith("etc/skel/.config/uwsm/env.d/90-omarchy-pi"):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(PACKAGED_RUNTIME_ENV, encoding="utf-8")
        destination.chmod(stat.S_IMODE(source.lstat().st_mode) & ~0o022)
        return
    _copy_entry(source, destination)


def _normalized_icon_name(relative: str) -> str:
    """Return a cache-safe hicolor basename for a source icon path.

    The upstream checkout keeps some legacy launcher artwork as names such as
    ``Disk Usage.png`` and ``Google Maps.png``.  GTK's icon cache parser treats
    spaces in hicolor basenames as malformed entries, while the desktop files
    already identify those applications with the normalized IDs
    ``disk-usage`` and ``google-maps``.  Keep the source checkout untouched and
    use those IDs for the installed theme files.
    """

    if Path(relative).parent != Path("."):
        raise RuntimePackageError(f"nested application icon path is unsupported: {relative}")
    filename = Path(relative).name
    stem = Path(filename).stem
    suffix = Path(filename).suffix.lower()
    normalized_stem = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    if not normalized_stem or not suffix:
        raise RuntimePackageError(f"cannot derive a hicolor icon name from {relative}")
    return f"{normalized_stem}{suffix}"


def _copy_mapped(
    source_root: Path,
    staging: Path,
    *,
    package: str,
    source_revision: str,
    package_version: str,
    source_sha256: str,
    channel: str,
) -> set[str]:
    paths: set[str] = set()
    if package == "omarchy-settings":
        for rel, source in _iter_files(source_root, "config"):
            if _path_allowed(rel, package=package):
                suffix = rel.removeprefix("config/")
                _copy_entry(source, staging / "usr/share/omarchy/config" / suffix)
                skel_destination = staging / "etc/skel/.config" / suffix
                if suffix == "hypr/hyprland.lua":
                    prefix = source_root / "install/arm64/session/fresh-hyprland-prefix.lua"
                    if prefix.is_file() and not prefix.is_symlink():
                        content = prefix.read_text(encoding="utf-8")
                        if not content.endswith("\n"):
                            content += "\n"
                        content += source.read_text(encoding="utf-8")
                        skel_destination.parent.mkdir(parents=True, exist_ok=True)
                        skel_destination.write_text(content, encoding="utf-8")
                        skel_destination.chmod(stat.S_IMODE(source.lstat().st_mode) & ~0o022)
                    else:
                        _copy_entry(source, skel_destination)
                else:
                    _copy_entry(source, skel_destination)
                paths.update({f"usr/share/omarchy/config/{suffix}", f"etc/skel/.config/{suffix}"})
        for rel, source in _iter_files(source_root, "default"):
            if _path_allowed(rel, package=package):
                suffix = rel.removeprefix("default/")
                _copy_entry(source, staging / "usr/share/omarchy/default" / suffix)
                paths.add(f"usr/share/omarchy/default/{suffix}")
        for rel, source in _iter_files(source_root, "applications"):
            if _path_allowed(rel, package=package):
                suffix = rel.removeprefix("applications/")
                if suffix.endswith(".desktop"):
                    _copy_entry(source, staging / "usr/share/omarchy/applications" / suffix)
                    _copy_entry(source, staging / "etc/skel/.local/share/applications" / suffix)
                    paths.update({f"usr/share/omarchy/applications/{suffix}", f"etc/skel/.local/share/applications/{suffix}"})
                elif "/" in suffix and suffix.startswith("icons/"):
                    icon = suffix.removeprefix("icons/")
                    installed_name = _normalized_icon_name(icon)
                    # An icon symlink moved out of the source tree would keep
                    # its old relative target.  Resolve it while staging so a
                    # package archive never contains a dangling relocated
                    # artwork link.
                    icon_source = source
                    if source.is_symlink():
                        try:
                            icon_source = source.resolve(strict=True)
                            icon_source.relative_to(source_root)
                        except (OSError, ValueError) as exc:
                            raise RuntimePackageError(f"icon symlink escapes source checkout: {rel}") from exc
                        if not icon_source.is_file():
                            raise RuntimePackageError(f"icon symlink target is not a file: {rel}")
                    for size in ICON_DESTINATION_SIZES:
                        destination_rel = f"usr/share/icons/hicolor/{size}/apps/{installed_name}"
                        destination = staging / destination_rel
                        if destination.exists() or destination.is_symlink():
                            raise RuntimePackageError(f"duplicate normalized icon name: {installed_name}")
                        _copy_entry(icon_source, destination)
                        paths.add(destination_rel)
        for source_rel, destination_rel in SAFE_ETC_FILES.items():
            source = source_root / source_rel
            if source.is_file() and not source.is_symlink():
                _copy_entry(source, staging / destination_rel)
                paths.add(destination_rel)
        for name in ("logo.txt", "logo.svg", "icon.txt", "icon.png"):
            source = source_root / name
            if source.is_file() and not source.is_symlink():
                _copy_entry(source, staging / "usr/share/omarchy" / name)
                paths.add(f"usr/share/omarchy/{name}")
        for source_rel, destination_rel in SAFE_PI_ASSETS.items():
            source = source_root / source_rel
            if source.is_file() and not source.is_symlink():
                destination = staging / destination_rel
                _copy_packaged_pi_asset(source, destination)
                if destination_rel.startswith("usr/libexec/") or destination_rel.endswith(".sh"):
                    destination.chmod(destination.stat().st_mode | 0o111)
                paths.add(destination_rel)
    else:
        for tree in ("shell", "install", "migrations", "themes"):
            for rel, source in _iter_files(source_root, tree):
                if _path_allowed(rel, package=package):
                    suffix = rel.removeprefix(tree + "/")
                    _copy_entry(source, staging / "usr/share/omarchy" / tree / suffix)
                    paths.add(f"usr/share/omarchy/{tree}/{suffix}")
        for rel, source in _iter_files(source_root, "bin"):
            if _path_allowed(rel, package=package):
                name = rel.removeprefix("bin/")
                _copy_entry(source, staging / "usr/bin" / name)
                command = staging / "usr/bin" / name
                if not command.is_symlink():
                    command.chmod(command.stat().st_mode | 0o111)
                share_link = staging / "usr/share/omarchy/bin" / name
                share_link.parent.mkdir(parents=True, exist_ok=True)
                share_link.symlink_to(f"../../../bin/{name}")
                paths.update({f"usr/bin/{name}", f"usr/share/omarchy/bin/{name}"})
        version = source_root / "version"
        if version.is_file() and not version.is_symlink():
            _copy_entry(version, staging / "usr/share/omarchy/version")
            paths.add("usr/share/omarchy/version")
        marker = staging / "usr/share/omarchy/.omarchy-pi-source-commit"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(source_revision + "\n", encoding="utf-8")
        marker.chmod(0o644)
        paths.add("usr/share/omarchy/.omarchy-pi-source-commit")
        packaged = staging / "usr/share/omarchy/.omarchy-pi-packaged.json"
        packaged.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "layout": "packaged",
                    # The provisioner accepts both the older ``layout`` name
                    # and this explicit mode field.  Emit both so a marker
                    # shipped by pacman is immediately consumable by the
                    # packaged-runtime handoff without being rewritten.
                    "runtime_mode": "packaged",
                    "channel": channel,
                    "version": package_version,
                    "source_revision": source_revision,
                    "source_sha256": source_sha256,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        packaged.chmod(0o644)
        paths.add("usr/share/omarchy/.omarchy-pi-packaged.json")
    return paths


def _assert_package_paths(paths: set[str], *, package: str) -> None:
    if not paths:
        raise RuntimePackageError(f"{package} has no package files")
    for path in paths:
        if path.startswith("boot/") or "/boot/" in path:
            raise RuntimePackageError(f"{package} owns a boot path: {path}")
        if any(token in path for token in ("etc/NetworkManager/", "etc/ssh/", "etc/sudoers", "etc/pam.d/", "etc/crypttab")):
            raise RuntimePackageError(f"{package} owns protected system path: {path}")
        if path.startswith("credentials/") or "/credentials/" in path:
            raise RuntimePackageError(f"{package} owns credentials: {path}")


def _pkginfo(spec: PackageSpec, version: str, source_revision: str, file_size: int) -> bytes:
    depends = list(spec.depends)
    return ("\n".join(
        [
            f"pkgname = {spec.name}",
            f"pkgbase = {spec.name}",
            f"pkgver = {version}",
            f"pkgdesc = {spec.description}",
            "url = https://github.com/omacom/omarchy",
            "builddate = 0",
            "packager = Omarchy Raspberry Pi port",
            f"size = {file_size}",
            f"arch = {ARCHITECTURE}",
            *[f"depend = {item.format(version=version)}" for item in depends],
            f"xdata = pkgtype=pkg",
            f"xdata = source-revision={source_revision}",
            "",
        ]
    )).encode()


def _mtree(staging: Path, paths: Iterable[str]) -> bytes:
    lines = ["#mtree", ". type=dir time=0 uid=0 gid=0 mode=0755"]
    # The archive contains every directory below staging (tarfile adds the
    # complete tree), so .MTREE must describe those directories too.  Without
    # these entries pacman can install the files but then warn that the
    # directory members are absent from the package file list.  Use lstat so a
    # symlink whose target happens to be a directory is still emitted only as
    # a link below.
    directories: list[str] = []
    for candidate in staging.rglob("*"):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(info.st_mode):
            directories.append(candidate.relative_to(staging).as_posix())
    for relative in sorted(directories):
        lines.append(f"./{relative} type=dir time=0 uid=0 gid=0 mode=0755")
    for relative in sorted(paths):
        path = staging / relative
        if path.is_symlink():
            lines.append(f"./{relative} type=link link={path.readlink()!s} uid=0 gid=0 mode=0777")
        elif path.is_file():
            digest = _sha256(path)
            mode = stat.S_IMODE(path.stat().st_mode)
            lines.append(f"./{relative} type=file size={path.stat().st_size} sha256digest={digest} uid=0 gid=0 mode={mode:04o}")
    return gzip.compress(("\n".join(lines) + "\n").encode(), mtime=0)


def _build_archive(staging: Path, archive: Path, spec: PackageSpec, version: str, source_revision: str, paths: set[str]) -> None:
    # Keep package metadata inside the archive and out of the package path
    # set.  Pacman and bsdtar both recognize these standard members.
    metadata = staging / ".PKGINFO"
    metadata.write_bytes(_pkginfo(spec, version, source_revision, sum((staging / p).lstat().st_size for p in paths)))
    metadata.chmod(0o644)
    (staging / ".BUILDINFO").write_text(
        "format = 1\n" f"pkgname = {spec.name}\n" f"pkgver = {version}\n" f"pkgarch = {ARCHITECTURE}\n" "builddate = 0\n" f"source_revision = {source_revision}\n",
        encoding="utf-8",
    )
    (staging / ".BUILDINFO").chmod(0o644)
    (staging / ".MTREE").write_bytes(_mtree(staging, paths))
    (staging / ".MTREE").chmod(0o644)

    def add_tree(stream: tarfile.TarFile, root: Path) -> None:
        entries = [root] + sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
        for entry in entries:
            arcname = "." if entry == root else entry.relative_to(root).as_posix()
            info = stream.gettarinfo(os.fspath(entry), arcname=arcname)
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            info.mtime = 0
            if info.isdir():
                info.mode = 0o755
            elif info.issym() or info.islnk():
                info.mode = 0o777
            else:
                info.mode = stat.S_IMODE(entry.stat().st_mode)
            if info.isreg():
                with entry.open("rb") as content:
                    stream.addfile(info, content)
            else:
                stream.addfile(info)

    raw_tar = archive.with_suffix(archive.suffix + ".tar")
    try:
        with raw_tar.open("wb") as raw:
            with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as stream:
                add_tree(stream, staging)
        _run(["zstd", "-q", "-19", "-T0", "-f", "-o", os.fspath(archive), os.fspath(raw_tar)])
    finally:
        raw_tar.unlink(missing_ok=True)


def _archive_members(archive: Path) -> list[tarfile.TarInfo]:
    try:
        result = _run(["zstd", "-q", "-d", "-c", os.fspath(archive)])
    except RuntimePackageError as exc:
        raise RuntimePackageError(f"cannot decompress package archive {archive.name}") from exc
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as stream:
        return stream.getmembers()


def _archive_member_bytes(archive: Path, name: str) -> bytes:
    result = _run(["bsdtar", "-xOf", os.fspath(archive), name])
    return result.stdout


def _parse_pkginfo(data: bytes) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for line in data.decode().splitlines():
        if " = " not in line:
            continue
        key, value = line.split(" = ", 1)
        fields.setdefault(key, []).append(value)
    return fields


def _make_manifest_row(name: str, version: str, source_revision: str, source_sha256: str, archive: Path) -> dict[str, object]:
    members = _archive_members(archive)
    files = sorted(
        member.name
        for member in members
        if not member.name.startswith(".") and member.name != "." and not member.isdir()
    )
    digest = _sha256(archive)
    return {
        "name": name,
        "package": name,
        "version": version,
        "architecture": ARCHITECTURE,
        "source_revision": source_revision,
        "source_sha256": source_sha256,
        "package_sha256": digest,
        "sha256": digest,
        "package_signature": "unsigned",
        "signature": None,
        "signature_sha256": None,
        "filename": archive.name,
        "archive_name": archive.name,
        "files": files,
    }


def _write_bundle_metadata(
    output: Path,
    rows: list[dict[str, object]],
    *,
    source_revision: str,
    source_sha256: str,
    channel: str,
) -> None:
    manifest_rows = [
        "\t".join(
            str(row[key])
            for key in ("package", "version", "architecture", "source_revision", "source_sha256", "package_sha256", "package_signature", "filename")
        )
        for row in rows
    ]
    (output / "manifest.tsv").write_text(
        "# schema_version=1\n# columns=package version architecture source_revision source_sha256 package_sha256 package_signature filename\n"
        + "\n".join(manifest_rows) + "\n",
        encoding="utf-8",
    )
    checksums = "".join(f"{row['package_sha256']}  {row['filename']}\n" for row in rows)
    for row in rows:
        if row["signature"] is not None:
            checksums += f"{row['signature_sha256']}  {row['signature']}\n"
    (output / "SHA256SUMS").write_text(checksums, encoding="utf-8")
    pair = {
        "schema_version": 1,
        "architecture": ARCHITECTURE,
        "version": rows[0]["version"],
        "channel": channel,
        "source": {"revision": source_revision, "archive_sha256": source_sha256},
        "packages": {
            str(row["package"]): {
                "filename": row["filename"],
                "sha256": row["package_sha256"],
                "signature": row["signature"],
                "signature_sha256": row["signature_sha256"],
                "files": row["files"],
            }
            for row in rows
        },
        "compatibility": "both packages must be installed from this pair; omarchy depends on the exact settings version",
        "protected_paths_excluded": list(FORBIDDEN_PATH_PARTS),
    }
    (output / "pair.json").write_text(json.dumps(pair, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # Keep a JSON copy under the conventional manifest name for existing
    # payload staging code, while pair.json remains the compatibility record.
    (output / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "architecture": ARCHITECTURE,
                "version": rows[0]["version"],
                "source_revision": source_revision,
                "source_sha256": source_sha256,
                "source": {"revision": source_revision, "archive_sha256": source_sha256},
                "channel": channel,
                "package_signatures": (
                    "unsigned"
                    if all(row["package_signature"] == "unsigned" for row in rows)
                    else "provided"
                ),
                "packages": rows,
                "archives": rows,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def build_bundle(
    source: Path,
    output: Path,
    *,
    version: str | None = None,
    source_revision: str | None = None,
    source_sha256: str | None = None,
    signature_dir: Path | None = None,
    channel: str = "stable",
) -> dict[str, object]:
    """Build a new package bundle and return the pair manifest."""

    source_text, revision = _validate_source(source, source_revision)
    source = Path(source_text)
    channel = _validate_channel(channel)
    version_file = _regular_file(source / "version", name="source version")
    if version is None:
        source_pkgver = version_file.read_text(encoding="utf-8").strip()
        requested_version = f"{source_pkgver}-{_source_revision_count(source, revision)}"
    else:
        requested_version = version
    package_version = _validate_version(requested_version)
    output = _absolute(output)
    _reject_symlink_components(output, include_leaf=False)
    if output.exists():
        if output.is_symlink() or not output.is_dir():
            raise RuntimePackageError("output must be a real directory")
        if any(output.iterdir()):
            raise RuntimePackageError("output directory must be empty")
    else:
        output.mkdir(parents=True)
    try:
        output.relative_to(source)
    except ValueError:
        pass
    else:
        raise RuntimePackageError("output must be outside the source checkout")
    if signature_dir is not None:
        signature_dir = _real_directory(signature_dir, name="signature directory")

    with tempfile.TemporaryDirectory(prefix="omarchy-runtime-source-") as temporary:
        archive = Path(temporary) / "source.tar"
        computed_sha256 = _archive_source(source, revision, archive)
        if source_sha256 is not None:
            _validate_sha256(source_sha256, name="source SHA-256")
            if source_sha256 != computed_sha256:
                raise RuntimePackageError("source SHA-256 does not match the pinned Git archive")
        source_sha256 = computed_sha256
        extracted = Path(temporary) / "source"
        _extract_archive(archive, extracted)
        rows: list[dict[str, object]] = []
        owned: set[str] = set()
        for name in PACKAGE_NAMES:
            staging = Path(temporary) / name
            staging.mkdir()
            paths = _copy_mapped(
                extracted,
                staging,
                package=name,
                source_revision=revision,
                package_version=package_version,
                source_sha256=source_sha256,
                channel=channel,
            )
            _assert_package_paths(paths, package=name)
            overlap = owned & paths
            if overlap:
                raise RuntimePackageError(f"package file overlap: {sorted(overlap)[0]}")
            owned.update(paths)
            archive_path = output / f"{name}-{package_version}-{ARCHITECTURE}.pkg.tar.zst"
            _build_archive(staging, archive_path, PACKAGE_SPECS[name], package_version, revision, paths)
            row = _make_manifest_row(name, package_version, revision, source_sha256, archive_path)
            if signature_dir is not None:
                signature_source = signature_dir / (archive_path.name + ".sig")
                if signature_source.exists() or signature_source.is_symlink():
                    signature_source = _regular_file(signature_source, name=f"signature for {name}")
                    signature_destination = output / signature_source.name
                    shutil.copy2(signature_source, signature_destination)
                    row["package_signature"] = "provided"
                    row["signature"] = signature_destination.name
                    row["signature_sha256"] = _sha256(signature_destination)
            rows.append(row)
    _write_bundle_metadata(
        output,
        rows,
        source_revision=revision,
        source_sha256=source_sha256,
        channel=channel,
    )
    return check_bundle(output)


def _read_tsv_manifest(output: Path) -> list[dict[str, str]]:
    manifest = _regular_file(output / "manifest.tsv", name="bundle manifest")
    rows: list[dict[str, str]] = []
    for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 8:
            raise RuntimePackageError(f"manifest line {line_number} has {len(fields)} columns")
        keys = ("package", "version", "architecture", "source_revision", "source_sha256", "package_sha256", "package_signature", "filename")
        rows.append(dict(zip(keys, fields)))
    return rows


def check_bundle(output: Path) -> dict[str, object]:
    """Validate native metadata, file ownership, checksums, and pair compatibility."""

    output = _real_directory(output, name="package bundle")
    rows = _read_tsv_manifest(output)
    if tuple(row["package"] for row in rows) != PACKAGE_NAMES:
        raise RuntimePackageError("manifest must contain omarchy-settings followed by omarchy")
    versions = {row["version"] for row in rows}
    revisions = {row["source_revision"] for row in rows}
    source_hashes = {row["source_sha256"] for row in rows}
    if len(versions) != 1 or len(revisions) != 1 or len(source_hashes) != 1:
        raise RuntimePackageError("package pair has incompatible versions or source provenance")
    version = next(iter(versions))
    _validate_version(version)
    revision = _validate_revision(next(iter(revisions)))
    source_sha256 = _validate_sha256(next(iter(source_hashes)), name="source SHA-256")
    manifest_path = _regular_file(output / "manifest.json", name="JSON bundle manifest")
    try:
        manifest_document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimePackageError("manifest.json is not valid JSON") from exc
    if not isinstance(manifest_document, dict) or manifest_document.get("schema_version") != 1:
        raise RuntimePackageError("manifest.json has an unsupported schema")
    channel = _validate_channel(str(manifest_document.get("channel", "")))
    if (
        manifest_document.get("architecture") != ARCHITECTURE
        or manifest_document.get("version") != version
        or manifest_document.get("source_revision") != revision
        or manifest_document.get("source_sha256") != source_sha256
        or manifest_document.get("source") != {"revision": revision, "archive_sha256": source_sha256}
    ):
        raise RuntimePackageError("manifest.json top-level provenance does not match the package pair")
    manifest_records = manifest_document.get("packages")
    if not isinstance(manifest_records, list):
        raise RuntimePackageError("manifest.json has no package records")
    records_by_package = {record.get("package"): record for record in manifest_records if isinstance(record, dict)}
    if set(records_by_package) != set(PACKAGE_NAMES):
        raise RuntimePackageError("manifest.json does not list both runtime packages")
    seen_files: set[str] = set()
    checked_rows: list[dict[str, object]] = []
    for row in rows:
        if row["architecture"] != ARCHITECTURE or row["package_signature"] not in {"unsigned", "provided"}:
            raise RuntimePackageError(f"unsupported package metadata for {row['package']}")
        record = records_by_package[row["package"]]
        if (
            record.get("name") != row["package"]
            or record.get("package") != row["package"]
            or record.get("filename") != row["filename"]
            or record.get("package_sha256") != row["package_sha256"]
            or record.get("sha256") != row["package_sha256"]
        ):
            raise RuntimePackageError(f"manifest.json disagrees with TSV metadata for {row['package']}")
        if row["package_signature"] == "unsigned":
            if record.get("signature") is not None or record.get("signature_sha256") is not None:
                raise RuntimePackageError(f"unsigned package has a signature record: {row['package']}")
        else:
            signature_name = record.get("signature")
            signature_sha256 = record.get("signature_sha256")
            if not isinstance(signature_name, str) or Path(signature_name).name != signature_name:
                raise RuntimePackageError(f"invalid signature filename for {row['package']}")
            _validate_sha256(signature_sha256, name="signature SHA-256")
            signature = _regular_file(output / signature_name, name="package signature")
            if _sha256(signature) != signature_sha256:
                raise RuntimePackageError(f"package signature checksum mismatch: {signature.name}")
        _validate_sha256(row["package_sha256"], name="package SHA-256")
        archive = _regular_file(output / row["filename"], name="package archive")
        if _sha256(archive) != row["package_sha256"]:
            raise RuntimePackageError(f"package checksum mismatch: {archive.name}")
        members = _archive_members(archive)
        names = {member.name for member in members}
        if ".PKGINFO" not in names:
            raise RuntimePackageError(f"package archive has no .PKGINFO: {archive.name}")
        info = _parse_pkginfo(_archive_member_bytes(archive, ".PKGINFO"))
        if info.get("pkgname") != [row["package"]] or info.get("pkgver") != [version] or info.get("arch") != [ARCHITECTURE]:
            raise RuntimePackageError(f"native package metadata disagrees with manifest: {archive.name}")
        files = {member.name for member in members if not member.name.startswith(".") and member.name != "." and not member.isdir()}
        protected = [path for path in files if any(path == part.rstrip("/") or path.startswith(part) for part in FORBIDDEN_PATH_PARTS)]
        if protected:
            raise RuntimePackageError(f"package archive owns protected path {protected[0]}")
        overlap = seen_files & files
        if overlap:
            raise RuntimePackageError(f"package archives overlap at {sorted(overlap)[0]}")
        seen_files.update(files)
        if row["package"] == "omarchy":
            dependency = f"omarchy-settings={version}"
            if dependency not in info.get("depend", []):
                raise RuntimePackageError("omarchy does not depend on the exact settings pair")
            marker = "usr/share/omarchy/.omarchy-pi-source-commit"
            if marker not in names:
                raise RuntimePackageError("omarchy has no source provenance marker")
            if _archive_member_bytes(archive, marker).decode().strip() != revision:
                raise RuntimePackageError("omarchy source provenance marker disagrees with pair manifest")
            packaged_marker = "usr/share/omarchy/.omarchy-pi-packaged.json"
            if packaged_marker not in names:
                raise RuntimePackageError("omarchy has no packaged runtime marker")
            try:
                packaged_document = json.loads(_archive_member_bytes(archive, packaged_marker).decode())
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimePackageError("packaged runtime marker is not valid JSON") from exc
            if packaged_document != {
                "schema_version": 1,
                "layout": "packaged",
                "runtime_mode": "packaged",
                "channel": channel,
                "version": version,
                "source_revision": revision,
                "source_sha256": source_sha256,
            }:
                raise RuntimePackageError("packaged runtime marker disagrees with package provenance")
        checked_rows.append({**row, "files": sorted(files)})

    pair_path = _regular_file(output / "pair.json", name="package pair manifest")
    try:
        pair = json.loads(pair_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimePackageError("pair.json is not valid JSON") from exc
    if not isinstance(pair, dict) or pair.get("schema_version") != 1:
        raise RuntimePackageError("pair.json has an unsupported schema")
    if pair.get("architecture") != ARCHITECTURE or pair.get("version") != version or pair.get("channel") != channel:
        raise RuntimePackageError("pair.json does not match the native package pair")
    source = pair.get("source")
    if not isinstance(source, dict) or source.get("revision") != revision or source.get("archive_sha256") != source_sha256:
        raise RuntimePackageError("pair.json source provenance does not match the native package pair")
    pairs = pair.get("packages")
    if not isinstance(pairs, dict) or set(pairs) != set(PACKAGE_NAMES):
        raise RuntimePackageError("pair.json does not list both runtime packages")
    for row in checked_rows:
        entry = pairs[row["package"]]
        if (
            not isinstance(entry, dict)
            or entry.get("filename") != row["filename"]
            or entry.get("sha256") != row["package_sha256"]
            or entry.get("signature") != records_by_package[row["package"]].get("signature")
            or entry.get("signature_sha256") != records_by_package[row["package"]].get("signature_sha256")
            or entry.get("files") != row["files"]
        ):
            raise RuntimePackageError(f"pair.json file record differs for {row['package']}")
    sums = _regular_file(output / "SHA256SUMS", name="bundle checksums")
    for row in rows:
        expected = f"{row['package_sha256']}  {row['filename']}"
        if expected not in sums.read_text(encoding="utf-8").splitlines():
            raise RuntimePackageError(f"SHA256SUMS does not contain {row['filename']}")
        signature_name = records_by_package[row["package"]].get("signature")
        if signature_name is not None:
            signature_sha256 = records_by_package[row["package"]].get("signature_sha256")
            if f"{signature_sha256}  {signature_name}" not in sums.read_text(encoding="utf-8").splitlines():
                raise RuntimePackageError(f"SHA256SUMS does not contain {signature_name}")
    return {
        "schema_version": 1,
        "architecture": ARCHITECTURE,
        "version": version,
        "channel": channel,
        "source_revision": revision,
        "source_sha256": source_sha256,
        "packages": checked_rows,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", "--source-checkout", type=Path, help="clean pinned Git checkout")
    parser.add_argument("--output", type=Path, help="new or empty package bundle directory")
    parser.add_argument("--version", help="pkgver-pkgrel (default: source version plus -1)")
    parser.add_argument("--source-revision", help="full 40-character source commit")
    parser.add_argument("--source-sha256", help="expected SHA-256 of the deterministic Git archive")
    parser.add_argument("--signature-dir", type=Path, help="directory containing caller-provided ARCHIVE.sig files")
    parser.add_argument("--channel", default="stable", help="runtime channel recorded in the packaged marker (default: stable)")
    parser.add_argument("--check", type=Path, metavar="BUNDLE", help="validate an existing package bundle")
    parser.add_argument("--json", action="store_true", help="print the resulting manifest as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    normalized = list(sys.argv[1:] if argv is None else argv)
    if normalized and normalized[0] == "build":
        normalized = normalized[1:]
    elif normalized and normalized[0] == "check":
        normalized = normalized[1:]
        if normalized and not normalized[0].startswith("-"):
            normalized = ["--check", normalized[0], *normalized[1:]]
    args = _parser().parse_args(normalized)
    try:
        if args.check is not None:
            result = check_bundle(args.check)
        else:
            if args.source is None or args.output is None:
                raise RuntimePackageError("--source and --output are required in build mode")
            result = build_bundle(
                args.source,
                args.output,
                version=args.version,
                source_revision=args.source_revision,
                source_sha256=args.source_sha256,
                signature_dir=args.signature_dir,
                channel=args.channel,
            )
    except RuntimePackageError as exc:
        print(f"build-runtime-packages: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Runtime package pair valid: {result['version']} ({result['architecture']})")
        print(f"Source: {result['source_revision']} ({result['source_sha256']})")
        print("Packages: " + ", ".join(row["package"] for row in result["packages"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
