#!/usr/bin/python3
"""Validate and stage the existing desktop bundle without accepting devices."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile

PAYLOAD_DIRECTORY = Path('/usr/local/share/omarchy-pi/desktop')
BUNDLE = PAYLOAD_DIRECTORY / 'desktop.tar.zst'
DESCRIPTOR = PAYLOAD_DIRECTORY / 'bundle.json'
WORK_DIRECTORY = Path('/var/lib/omarchy-pi/installer/payload')
MIB = 1024 * 1024


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
                    elif parts[1] == 'desktop-manifest.json':
                        if len(parts) != 2 or member.size > MIB:
                            raise PayloadError('desktop manifest has an invalid size/path')
                        manifest = json.load(archive.extractfile(member))
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
    if not root_bytes or not source_bytes:
        raise PayloadError('desktop root/source is empty')
    return {'schema_version': 1, 'sha256': expected_sha256, 'source_revision': revision,
            'archive_prefix': prefix, 'bundle_bytes': bundle.stat().st_size,
            'root_bytes': root_bytes, 'source_bytes': source_bytes, 'boot_bytes': boot_bytes,
            'unpacked_bytes': root_bytes + source_bytes + boot_bytes,
            'required_target_bytes': root_bytes + source_bytes + 1024 * MIB}


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


def prepare_payload(bundle: Path = BUNDLE, descriptor: Path = DESCRIPTOR, work: Path = WORK_DIRECTORY) -> Path:
    metadata = payload_metadata(bundle, descriptor)
    work = Path(work)
    if work.is_symlink() or work.resolve() == Path('/'):
        raise PayloadError('invalid payload work directory')
    complete = work / '.bundle-sha256'
    if complete.is_file() and not complete.is_symlink() and complete.read_text().strip() == metadata['sha256']:
        validate_generic(work, metadata)
        return work
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
               '--strip-components=1', '--numeric-owner', '--same-owner', '--same-permissions', '--acls', '--xattrs',
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
    return work


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
