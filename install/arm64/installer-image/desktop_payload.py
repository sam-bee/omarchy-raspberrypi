#!/usr/bin/python3
"""Validate and stage the existing desktop bundle without accepting devices."""
from __future__ import annotations

import hashlib
import gzip
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from io import BytesIO

PAYLOAD_DIRECTORY = Path('/usr/local/share/omarchy-pi/desktop')
BUNDLE = PAYLOAD_DIRECTORY / 'desktop.tar.zst'
DESCRIPTOR = PAYLOAD_DIRECTORY / 'bundle.json'
WORK_DIRECTORY = Path('/var/lib/omarchy-pi/installer/payload')
MIB = 1024 * 1024
RUNTIME_PACKAGES = ('omarchy-settings', 'omarchy')
RUNTIME_ARCHIVE_MAX_BYTES = 256 * MIB
ARCHIVE_READ_CHUNK = 4 * MIB
RUNTIME_ARCHIVE_NAME = re.compile(
    r'(?:omarchy-settings|omarchy)-.+-aarch64\.pkg\.tar\.[A-Za-z0-9]+\Z'
)


class PayloadError(RuntimeError):
    pass


def regular_file(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise PayloadError('payload path contains a symlink')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not info.st_size:
        raise PayloadError('payload must be a nonempty regular file')
    return path


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with regular_file(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * MIB), b''):
            digest.update(block)
    return digest.hexdigest()


def _is_runtime_archive(name: str) -> bool:
    """Recognize the two conventional aarch64 Omarchy package filenames."""

    return bool(RUNTIME_ARCHIVE_NAME.fullmatch(name))


def _hash_member(stream, *, retain: bool) -> tuple[str, bytes | None]:
    """Hash a tar member incrementally, retaining only bounded runtime data."""

    digest = hashlib.sha256()
    retained = BytesIO() if retain else None
    total = 0
    while True:
        block = stream.read(ARCHIVE_READ_CHUNK)
        if not block:
            break
        total += len(block)
        if retained is not None:
            if total > RUNTIME_ARCHIVE_MAX_BYTES:
                raise PayloadError('runtime package archive exceeds the 256 MiB validation limit')
            retained.write(block)
        digest.update(block)
    return digest.hexdigest(), retained.getvalue() if retained is not None else None


def _package_contents(data: bytes) -> tuple[dict[str, str], set[str]]:
    """Return native package metadata and its owned non-directory paths."""

    try:
        result = subprocess.run(
            ['zstd', '--decompress', '--stdout', '--'],
            input=data,
            check=True,
            capture_output=True,
        )
        stream = tarfile.open(fileobj=BytesIO(result.stdout), mode='r:')
    except (OSError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        raise PayloadError('runtime package archive is unreadable') from exc
    metadata: dict[str, str] = {}
    owned: set[str] = set()
    mtree_owned: set[str] = set()
    try:
        for member in stream:
            if member.name in {'.PKGINFO', '.BUILDINFO'}:
                if not member.isreg() or member.size > MIB:
                    raise PayloadError('runtime package metadata has an invalid member')
                content = stream.extractfile(member)
                if content is None:
                    raise PayloadError('runtime package metadata is unreadable')
                for line in content.read().decode('utf-8').splitlines():
                    if ' = ' in line:
                        key, value = line.split(' = ', 1)
                        if key == 'xdata' and '=' in value:
                            xdata_key, xdata_value = value.split('=', 1)
                            metadata[xdata_key] = xdata_value
                        else:
                            metadata[key] = value
            elif member.name == '.MTREE':
                if not member.isreg() or member.size > MIB:
                    raise PayloadError('runtime package ownership metadata has an invalid member')
                content = stream.extractfile(member)
                if content is None:
                    raise PayloadError('runtime package ownership metadata is unreadable')
                try:
                    for line in gzip.decompress(content.read()).decode('utf-8').splitlines():
                        if not line.startswith('./') or ' type=' not in line:
                            continue
                        path, attributes = line.split(' ', 1)
                        if 'type=dir' not in attributes:
                            mtree_owned.add(path[2:])
                except (OSError, UnicodeDecodeError) as exc:
                    raise PayloadError('runtime package ownership metadata is malformed') from exc
            elif member.name.startswith('.'):
                continue
            elif member.name == '.' or member.isdir():
                continue
            else:
                path = PurePosixPath(member.name)
                if path.is_absolute() or '..' in path.parts or '.' in path.parts:
                    raise PayloadError('runtime package owns an unsafe path')
                owned.add(member.name)
    except (UnicodeDecodeError, tarfile.TarError) as exc:
        raise PayloadError('runtime package metadata is malformed') from exc
    finally:
        stream.close()
    if not mtree_owned or mtree_owned != owned:
        raise PayloadError('runtime package ownership metadata differs from its archive')
    return metadata, owned


def _validate_runtime_manifest(manifest: dict, package_archives: dict[str, tuple[str, bytes]]) -> dict:
    runtime = manifest.get('runtime')
    if not isinstance(runtime, dict) or runtime.get('layout') != 'packaged':
        raise PayloadError('packaged desktop manifest has no runtime layout')
    if runtime.get('path') != '/usr/share/omarchy':
        raise PayloadError('packaged runtime path is not conventional')
    revision = manifest.get('source', {}).get('revision')
    if runtime.get('source_revision') != revision:
        raise PayloadError('packaged runtime source revision differs from the desktop source')
    records = runtime.get('packages')
    if not isinstance(records, list) or [record.get('name') for record in records if isinstance(record, dict)] != list(RUNTIME_PACKAGES):
        raise PayloadError('packaged runtime manifest must list the Omarchy pair in order')
    seen: set[str] = set()
    versions: set[str] = set()
    source_hashes: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise PayloadError('packaged runtime package record is malformed')
        package = record.get('name')
        if package in seen or package not in RUNTIME_PACKAGES:
            raise PayloadError('packaged runtime package names are invalid')
        seen.add(package)
        if record.get('package') != package or record.get('architecture') != 'aarch64':
            raise PayloadError(f'packaged runtime metadata is invalid for {package}')
        version = record.get('version')
        if not isinstance(version, str) or not version or any(character.isspace() for character in version):
            raise PayloadError(f'packaged runtime version is invalid for {package}')
        versions.add(version)
        filename = record.get('filename')
        if not isinstance(filename, str) or PurePosixPath(filename).name != filename or filename not in package_archives:
            raise PayloadError(f'packaged runtime archive is missing for {package}')
        digest, data = package_archives[filename]
        if record.get('sha256') != digest or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise PayloadError(f'packaged runtime archive hash differs for {package}')
        if record.get('source_revision') != revision:
            raise PayloadError(f'packaged runtime source provenance differs for {package}')
        source_sha256 = record.get('source_sha256')
        if not isinstance(source_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', source_sha256):
            raise PayloadError(f'packaged runtime source archive hash is invalid for {package}')
        source_hashes.add(source_sha256)
        files = record.get('files')
        if not isinstance(files, list) or not files or any(
            not isinstance(path, str) or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts
            for path in files
        ):
            raise PayloadError(f'packaged runtime ownership records are invalid for {package}')
        native, owned = _package_contents(data)
        if native.get('pkgname') != package or native.get('pkgver') != version or native.get('arch') != 'aarch64':
            raise PayloadError(f'packaged runtime native metadata differs for {package}')
        if native.get('pkgtype') != 'pkg' or native.get('source-revision') != revision:
            raise PayloadError(f'packaged runtime native provenance differs for {package}')
        if set(files) != owned:
            raise PayloadError(f'packaged runtime ownership records differ for {package}')
        if package == 'omarchy':
            marker = 'usr/share/omarchy/.omarchy-pi-source-commit'
            if marker not in owned:
                raise PayloadError('omarchy package has no source provenance marker')
            marker_data = None
            try:
                package_stream = tarfile.open(fileobj=BytesIO(subprocess.run(
                    ['zstd', '--decompress', '--stdout', '--'], input=data, check=True, capture_output=True
                ).stdout), mode='r:')
                marker_member = package_stream.getmember(marker)
                marker_file = package_stream.extractfile(marker_member)
                marker_data = marker_file.read().decode('utf-8').strip() if marker_file else None
                package_stream.close()
            except (OSError, subprocess.CalledProcessError, KeyError, tarfile.TarError, UnicodeDecodeError):
                raise PayloadError('omarchy package source marker is unreadable') from None
            if marker_data != revision:
                raise PayloadError('omarchy package source marker differs from the desktop source')
    if len(versions) != 1:
        raise PayloadError('packaged runtime pair has incompatible versions')
    if len(source_hashes) != 1:
        raise PayloadError('packaged runtime pair has incompatible source hashes')
    return {'layout': 'packaged', 'path': '/usr/share/omarchy', 'source_revision': revision, 'packages': records}


def inspect_bundle(bundle: Path, expected_sha256: str) -> dict:
    if not re.fullmatch(r'[0-9a-f]{64}', expected_sha256):
        raise PayloadError('desktop SHA-256 must be 64 lowercase hex characters')
    bundle = regular_file(bundle)
    if digest_file(bundle) != expected_sha256:
        raise PayloadError('desktop bundle checksum differs')
    process = subprocess.Popen(['zstd', '--decompress', '--stdout', '--', str(bundle)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    prefix = None
    manifest = None
    seen: set[str] = set()
    links: set[str] = set()
    root_bytes = source_bytes = boot_bytes = 0
    package_archives: dict[str, tuple[str, bytes]] = {}
    try:
        with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
            for member in archive:
                name = member.name.rstrip('/')
                path = PurePosixPath(name)
                parts = path.parts
                if not parts or path.is_absolute() or '..' in parts or '.' in parts:
                    raise PayloadError('desktop archive contains an unsafe path')
                if name in seen:
                    raise PayloadError('desktop archive contains duplicate paths')
                seen.add(name)
                if prefix is None:
                    prefix = parts[0]
                if parts[0] != prefix or (len(parts) > 1 and parts[1] not in {'rootfs', 'source', 'packages', 'desktop-manifest.json'}):
                    raise PayloadError('desktop archive has an unexpected layout')
                if not (member.isdir() or member.isreg() or member.issym() or member.islnk()):
                    raise PayloadError('desktop archive contains a special device/file')
                if member.issym():
                    links.add(name)
                if member.islnk():
                    target = PurePosixPath(member.linkname)
                    if target.is_absolute() or '..' in target.parts or not target.parts or target.parts[0] != prefix:
                        raise PayloadError('desktop archive has an unsafe hard link')
                if member.isreg() and len(parts) > 1:
                    if parts[1] == 'rootfs':
                        if len(parts) > 2 and parts[2] == 'boot':
                            boot_bytes += member.size
                        else:
                            root_bytes += member.size
                    elif parts[1] == 'source':
                        source_bytes += member.size
                    elif parts[1] == 'packages':
                        if len(parts) != 3:
                            raise PayloadError('desktop package payload has an invalid path')
                        package_name = parts[2]
                        retain = _is_runtime_archive(package_name)
                        if retain and member.size > RUNTIME_ARCHIVE_MAX_BYTES:
                            raise PayloadError('runtime package archive exceeds the 256 MiB validation limit')
                        content = archive.extractfile(member)
                        if content is None:
                            raise PayloadError('desktop package payload is unreadable')
                        digest, data = _hash_member(content, retain=retain)
                        if data is not None:
                            package_archives[package_name] = (digest, data)
                    elif parts[1] == 'desktop-manifest.json':
                        if len(parts) != 2 or member.size > MIB:
                            raise PayloadError('desktop manifest has an invalid size/path')
                        manifest = json.load(archive.extractfile(member))
        # tar stops at its end marker before zstd has necessarily written
        # archive padding. Drain the pipe before waiting, including on hosts
        # with a small pipe buffer, so the decompressor cannot deadlock.
        while process.stdout.read(MIB):
            pass
        if process.wait() != 0:
            raise PayloadError('desktop bundle decompression failed')
    except (tarfile.TarError, ValueError, OSError) as exc:
        raise PayloadError('desktop bundle is unreadable or malformed') from exc
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdout:
            process.stdout.close()
    if any(any(str(parent) in links for parent in PurePosixPath(name).parents) for name in seen):
        raise PayloadError('desktop archive writes through a symlink')
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1:
        raise PayloadError('desktop manifest schema is unsupported')
    revision = manifest.get('source', {}).get('revision', '')
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise PayloadError('desktop source revision is invalid')
    if manifest.get('target', {}).get('architecture') != 'aarch64' or manifest.get('target', {}).get('profile') != 'rpi5':
        raise PayloadError('desktop payload is not for aarch64 Pi 5')
    runtime = None
    if 'runtime' in manifest:
        runtime = _validate_runtime_manifest(manifest, package_archives)
    if not root_bytes or not source_bytes:
        raise PayloadError('desktop root/source is empty')
    metadata = {'schema_version': 1, 'sha256': expected_sha256, 'source_revision': revision,
            'archive_prefix': prefix, 'bundle_bytes': bundle.stat().st_size,
            'root_bytes': root_bytes, 'source_bytes': source_bytes, 'boot_bytes': boot_bytes,
            'unpacked_bytes': root_bytes + source_bytes + boot_bytes,
            'required_target_bytes': root_bytes + source_bytes + 1024 * MIB}
    if runtime is not None:
        metadata['runtime'] = runtime
    return metadata


def stage_bundle(target: Path, bundle: Path, expected_sha256: str) -> dict:
    target = Path(target)
    if target.resolve() == Path('/') or target.is_symlink():
        raise PayloadError('refusing live root for bundle staging')
    metadata = inspect_bundle(bundle, expected_sha256)
    destination = target / PAYLOAD_DIRECTORY.relative_to('/')
    destination.mkdir(parents=True, mode=0o755, exist_ok=False)
    shutil.copyfile(bundle, destination / BUNDLE.name)
    (destination / BUNDLE.name).chmod(0o644)
    (destination / DESCRIPTOR.name).write_text(json.dumps(metadata, indent=2, sort_keys=True) + '\n')
    (destination / DESCRIPTOR.name).chmod(0o644)
    return metadata


def payload_metadata(bundle: Path = BUNDLE, descriptor: Path = DESCRIPTOR) -> dict:
    metadata = json.loads(regular_file(descriptor).read_text())
    if not isinstance(metadata, dict):
        raise PayloadError('invalid bundle descriptor')
    actual = inspect_bundle(bundle, metadata.get('sha256', ''))
    if actual != metadata:
        raise PayloadError('desktop bundle descriptor differs from its contents')
    return actual


def validate_generic(payload: Path, metadata: dict) -> None:
    root = payload / 'rootfs'
    for relative in ('etc/hostname', 'etc/machine-id'):
        path = root / relative
        if path.is_symlink() or (path.exists() and path.read_text().strip()):
            raise PayloadError('desktop root contains a machine identity')
    if any((root / 'etc/ssh').glob('ssh_host_*')):
        raise PayloadError('desktop root contains SSH host keys')
    if list((root / 'home').iterdir()):
        raise PayloadError('desktop root contains a user home')
    passwd = (root / 'etc/passwd').read_text().splitlines()
    for line in passwd:
        fields = line.split(':')
        if len(fields) != 7 or fields[0] == 'alarm' or (int(fields[2]) >= 1000 and fields[0] != 'nobody'):
            raise PayloadError('desktop root contains a login account')
    root_entries = [line.split(':') for line in (root / 'etc/shadow').read_text().splitlines() if line.startswith('root:')]
    if len(root_entries) != 1 or not root_entries[0][1].startswith(('!', '*')):
        raise PayloadError('desktop root contains a stock root password')
    for relative in ('etc/pacman.d/gnupg', 'boot/installer-settings.toml', 'usr/lib/omarchy-pi/installer-image.marker'):
        if os.path.lexists(root / relative):
            raise PayloadError('desktop root contains private build or installer state')
    source = payload / 'source'
    result = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True)
    if result.stdout.strip() != metadata['source_revision']:
        raise PayloadError('desktop source checkout differs from manifest')
    result = subprocess.run(['git', '-C', str(source), 'status', '--porcelain', '--untracked-files=all'], check=True, capture_output=True, text=True)
    if result.stdout.strip():
        raise PayloadError('desktop source checkout is modified')
    runtime = metadata.get('runtime')
    if runtime is not None:
        marker = root / 'usr/share/omarchy/.omarchy-pi-source-commit'
        if marker.is_symlink() or not marker.is_file() or marker.read_text(encoding='utf-8').strip() != metadata['source_revision']:
            raise PayloadError('packaged Omarchy source marker differs from the desktop source')


def prepare_payload(bundle: Path = BUNDLE, descriptor: Path = DESCRIPTOR, work: Path = WORK_DIRECTORY) -> tuple[Path, dict]:
    """Inspect once, prepare the root, and retain the verified bundle facts."""
    metadata = payload_metadata(bundle, descriptor)
    work = Path(work)
    if work.is_symlink() or work.resolve() == Path('/'):
        raise PayloadError('invalid payload work directory')
    complete = work / '.bundle-sha256'
    if complete.is_file() and not complete.is_symlink() and complete.read_text().strip() == metadata['sha256']:
        validate_generic(work, metadata)
        return work, metadata
    if os.path.lexists(work):
        raise PayloadError('incomplete payload work directory; retain it and choose a fresh installer image')
    work.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if shutil.disk_usage(work.parent).free < metadata['unpacked_bytes'] + 256 * MIB:
        raise PayloadError('installer has insufficient space to unpack desktop')
    work.mkdir(mode=0o700)
    (work / '.extracting').write_text(metadata['sha256'] + '\n')
    (work / '.extracting').chmod(0o600)
    prefix = metadata['archive_prefix']
    command = ['tar', '--zstd', '--extract', '--file', str(bundle), '--directory', str(work),
               '--strip-components=1', '--numeric-owner', '--same-owner', '--same-permissions', '--acls', '--xattrs', '--xattrs-include=*',
               '--', f'{prefix}/rootfs', f'{prefix}/source', f'{prefix}/desktop-manifest.json']
    result = subprocess.run(command, check=False, capture_output=True)
    if result.returncode:
        raise PayloadError('desktop extraction failed; partial work retained')
    # Source is public input; account ownership belongs only to rootfs.
    for current, dirs, files in os.walk(work / 'source'):
        for name in ['.', *dirs, *files]:
            os.chown(Path(current) / name, os.geteuid(), os.getegid(), follow_symlinks=False)
    validate_generic(work, metadata)
    complete.write_text(metadata['sha256'] + '\n')
    complete.chmod(0o600)
    (work / '.extracting').unlink()
    return work, metadata


def verify_prepared_bundle(metadata: dict, bundle: Path = BUNDLE) -> None:
    """Before erasure, check the inspected bytes still match without re-expanding the archive."""
    if digest_file(bundle) != metadata['sha256']:
        raise PayloadError('desktop bundle changed after preparation')


def reset_incomplete_payload() -> None:
    """Discard only our recognised partial extraction after confirmed restart."""
    work = WORK_DIRECTORY
    if not os.path.lexists(work) or (work / '.bundle-sha256').is_file():
        return
    for path in (work, *work.parents):
        if path.is_symlink():
            raise PayloadError('refusing redirected partial extraction')
    marker = work / '.extracting'
    info = regular_file(marker).stat()
    metadata = payload_metadata()
    if work.stat().st_uid != 0 or info.st_uid != 0 or info.st_mode & 0o077 or marker.read_text().strip() != metadata['sha256']:
        raise PayloadError('partial extraction does not belong to this installer')
    shutil.rmtree(work)


def copy_payload(payload: Path, root: Path, boot: Path) -> None:
    """Populate the freshly mounted target, preserving root metadata."""
    payload, root, boot = (Path(os.path.abspath(path)) for path in (payload, root, boot))
    for path in (payload, root, boot):
        if path == Path('/') or not path.is_dir():
            raise PayloadError('payload copy requires ordinary mounted directories')
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise PayloadError('payload copy path contains a symlink')
    if boot != root / 'boot' or payload == root or root in payload.parents or payload in root.parents:
        raise PayloadError('payload copy directories overlap or boot is misplaced')
    if set(entry.name for entry in root.iterdir()) - {'lost+found', 'boot'} or any(boot.iterdir()):
        raise PayloadError('target filesystems are not empty')
    source_root = payload / 'rootfs'
    if source_root.is_symlink() or not source_root.is_dir():
        raise PayloadError('prepared payload has no root tree')
    # These helpers operate only on trees; the assembler's block-device
    # commands and image creation entry points are never called here.
    specification = importlib.util.spec_from_file_location('installer_payload_assembler', Path(__file__).with_name('assemble-image.py'))
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    try:
        module.copy_root_without_boot(source_root, root)
        module.copy_boot_tree(source_root / 'boot', boot)
    except module.ImageAssemblyError as exc:
        raise PayloadError(str(exc)) from exc
