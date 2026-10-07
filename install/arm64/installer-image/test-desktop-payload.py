#!/usr/bin/python3
import hashlib
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import desktop_payload as payload


class PayloadTests(unittest.TestCase):
    @staticmethod
    def mtree_path(name):
        return ''.join(
            f'\\{byte:03o}' if byte <= 0x20 or byte == 0x5C or byte >= 0x7F else chr(byte)
            for byte in name.encode('utf-8')
        )

    def runtime_package(self, directory, package, revision, files):
        raw = Path(directory) / f"{package}-1.0-1-aarch64.pkg.tar"
        with tarfile.open(raw, 'w') as stream:
            metadata = (
                f"pkgname = {package}\n"
                f"pkgbase = {package}\n"
                "pkgver = 1.0-1\n"
                "arch = aarch64\n"
                "xdata = pkgtype=pkg\n"
                f"xdata = source-revision={revision}\n"
            ).encode()
            ownership = '#mtree\n. type=dir time=0 uid=0 gid=0 mode=0755\n' + ''.join(
                f'./{self.mtree_path(name)} type={"link" if isinstance(data, str) else "file"}\n'
                for name, data in files.items()
            )
            for name, data in (
                ('.PKGINFO', metadata),
                ('.BUILDINFO', b'fixture\n'),
                ('.MTREE', gzip.compress(ownership.encode())),
            ):
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                stream.addfile(entry, io.BytesIO(data))
            for name, data in files.items():
                entry = tarfile.TarInfo(name)
                if isinstance(data, str):
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = data
                    stream.addfile(entry)
                else:
                    entry.size = len(data)
                    stream.addfile(entry, io.BytesIO(data))
        archive = raw.with_suffix(raw.suffix + '.zst')
        subprocess.run(['zstd', '-q', '-o', str(archive), str(raw)], check=True)
        return archive

    def packaged_bundle(self, directory, *, stale_marker=False):
        revision = 'a' * 40
        source_sha256 = 'b' * 64
        settings_files = {'usr/share/omarchy/config/fixture': b'settings'}
        runtime_files = {
            'usr/share/omarchy/.omarchy-pi-source-commit': (('c' if stale_marker else 'a') * 40).encode(),
            'usr/bin/omarchy': b'#!/bin/bash\n',
        }
        settings = self.runtime_package(directory, 'omarchy-settings', revision, settings_files)
        runtime = self.runtime_package(directory, 'omarchy', revision, runtime_files)
        generic = Path(directory) / 'glibc-2.0-1-aarch64.pkg.tar.zst'
        generic.write_bytes(b'generic package bytes')
        records = []
        for package, archive, files in (
            ('omarchy-settings', settings, settings_files),
            ('omarchy', runtime, runtime_files),
        ):
            records.append({
                'name': package,
                'package': package,
                'version': '1.0-1',
                'architecture': 'aarch64',
                'filename': archive.name,
                'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                'source_revision': revision,
                'source_sha256': source_sha256,
                'files': sorted(files),
                'package_signature': 'unsigned',
                'signature': None,
                'signature_sha256': None,
            })
        manifest = {
            'schema_version': 1,
            'source': {'revision': revision},
            'target': {'architecture': 'aarch64', 'profile': 'rpi5'},
            'runtime': {
                'layout': 'packaged',
                'path': '/usr/share/omarchy',
                'source_revision': revision,
                'packages': records,
            },
        }
        archive_path = Path(directory) / 'packaged.tar'
        with tarfile.open(archive_path, 'w') as stream:
            for name in ('bundle', 'bundle/rootfs', 'bundle/rootfs/etc', 'bundle/rootfs/usr/share/omarchy', 'bundle/source', 'bundle/source/.git', 'bundle/packages'):
                entry = tarfile.TarInfo(name)
                entry.type = tarfile.DIRTYPE
                stream.addfile(entry)
            files = {
                'bundle/desktop-manifest.json': json.dumps(manifest).encode(),
                'bundle/rootfs/etc/passwd': b'root:x:0:0:root:/root:/bin/bash\n',
                'bundle/rootfs/usr/share/omarchy/.omarchy-pi-source-commit': (('c' if stale_marker else 'a') * 40 + '\n').encode(),
                'bundle/source/.git/HEAD': b'fake-source',
            }
            for name, data in files.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                stream.addfile(entry, io.BytesIO(data))
            for package in (settings, runtime, generic):
                data = package.read_bytes()
                entry = tarfile.TarInfo(f'bundle/packages/{package.name}')
                entry.size = len(data)
                stream.addfile(entry, io.BytesIO(data))
        bundle = archive_path.with_suffix('.tar.zst')
        subprocess.run(['zstd', '-q', '-o', str(bundle), str(archive_path)], check=True)
        return bundle, hashlib.sha256(bundle.read_bytes()).hexdigest()

    def bundle(self, directory, *, unsafe=False, symlink_child=False):
        archive = Path(directory) / 'payload.tar'
        manifest = {'schema_version': 1, 'source': {'revision': 'a' * 40},
                    'target': {'architecture': 'aarch64', 'profile': 'rpi5'}}
        with tarfile.open(archive, 'w') as stream:
            for name in ('bundle', 'bundle/rootfs', 'bundle/rootfs/etc', 'bundle/source', 'bundle/source/.git'):
                entry = tarfile.TarInfo(name)
                entry.type = tarfile.DIRTYPE
                entry.mode = 0o755
                entry.uid, entry.gid = os.getuid(), os.getgid()
                stream.addfile(entry)
            files = {'bundle/desktop-manifest.json': json.dumps(manifest).encode(),
                     'bundle/rootfs/etc/passwd': b'root:x:0:0:root:/root:/bin/bash\n',
                     'bundle/source/.git/HEAD': b'fake-source'}
            if unsafe:
                files['bundle/rootfs/../../outside'] = b'escape'
            if symlink_child:
                link = tarfile.TarInfo('bundle/rootfs/link')
                link.type = tarfile.SYMTYPE
                link.linkname = '/etc'
                stream.addfile(link)
                files['bundle/rootfs/link/passwd'] = b'unsafe'
            for name, data in files.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                entry.uid, entry.gid = os.getuid(), os.getgid()
                stream.addfile(entry, io.BytesIO(data))
        bundle = archive.with_suffix('.tar.zst')
        subprocess.run(['zstd', '-q', '-o', str(bundle), str(archive)], check=True)
        return bundle, hashlib.sha256(bundle.read_bytes()).hexdigest()

    def test_metadata_sizes_and_source_are_verified_without_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle, digest = self.bundle(directory)
            info = payload.inspect_bundle(bundle, digest)
            self.assertEqual(info['source_revision'], 'a' * 40)
            self.assertGreater(info['required_target_bytes'], info['unpacked_bytes'])
            self.assertFalse((Path(directory) / 'bundle').exists())

    def test_checksum_mismatch_refuses_before_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle, _ = self.bundle(directory)
            root = Path(directory) / 'root'
            root.mkdir()
            with self.assertRaises(payload.PayloadError):
                payload.stage_bundle(root, bundle, '0' * 64)
            self.assertEqual(list(root.iterdir()), [])

    def test_unsafe_names_and_symlink_descendants_are_rejected(self):
        for kwargs in ({'unsafe': True}, {'symlink_child': True}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as directory:
                bundle, digest = self.bundle(directory, **kwargs)
                with self.assertRaises(payload.PayloadError):
                    payload.inspect_bundle(bundle, digest)

    def test_runtime_descriptor_cannot_claim_other_source(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle, digest = self.bundle(directory)
            descriptor = Path(directory) / 'bundle.json'
            info = payload.inspect_bundle(bundle, digest)
            info['source_revision'] = 'b' * 40
            descriptor.write_text(json.dumps(info))
            with self.assertRaises(payload.PayloadError):
                payload.payload_metadata(bundle, descriptor)

    def test_packaged_runtime_pair_and_ownership_records_are_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle, digest = self.packaged_bundle(directory)
            info = payload.inspect_bundle(bundle, digest)
            self.assertEqual(info['runtime']['layout'], 'packaged')
            self.assertEqual(info['runtime']['source_revision'], 'a' * 40)

    def test_mtree_octal_paths_match_owned_paths_with_spaces_and_backslashes(self):
        with tempfile.TemporaryDirectory() as directory:
            revision = 'a' * 40
            files = {
                'usr/share/omarchy/Disk Usage.desktop': b'desktop entry',
                r'usr/share/omarchy/back\slash': b'backslash path',
                'usr/share/omarchy/café.desktop': b'non-ascii path',
            }
            archive = self.runtime_package(directory, 'omarchy-settings', revision, files)
            native, owned = payload._package_contents(archive.read_bytes())
            self.assertEqual(native['pkgname'], 'omarchy-settings')
            self.assertEqual(owned, set(files))

    def test_non_runtime_package_bytes_are_not_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle, digest = self.packaged_bundle(directory)
            retained = {}

            def capture(manifest, package_archives):
                retained.update(package_archives)
                return manifest['runtime']

            with mock.patch.object(payload, '_validate_runtime_manifest', side_effect=capture):
                payload.inspect_bundle(bundle, digest)
            self.assertEqual(
                set(retained),
                {'omarchy-settings-1.0-1-aarch64.pkg.tar.zst', 'omarchy-1.0-1-aarch64.pkg.tar.zst'},
            )
            self.assertNotIn('glibc-2.0-1-aarch64.pkg.tar.zst', retained)

    def test_runtime_archive_retention_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle, digest = self.packaged_bundle(directory)
            original = payload.RUNTIME_ARCHIVE_MAX_BYTES
            payload.RUNTIME_ARCHIVE_MAX_BYTES = 8
            self.addCleanup(setattr, payload, 'RUNTIME_ARCHIVE_MAX_BYTES', original)
            with self.assertRaisesRegex(payload.PayloadError, '256 MiB validation limit'):
                payload.inspect_bundle(bundle, digest)

    def test_archive_hashing_reads_in_fixed_chunks_without_retaining_generic_data(self):
        class Reader:
            def __init__(self, data):
                self.data = data
                self.offset = 0
                self.requests = []

            def read(self, size):
                self.requests.append(size)
                block = self.data[self.offset:self.offset + size]
                self.offset += len(block)
                return block

        reader = Reader(b'generic archive bytes')
        digest, retained = payload._hash_member(reader, retain=False)
        self.assertEqual(digest, hashlib.sha256(b'generic archive bytes').hexdigest())
        self.assertIsNone(retained)
        self.assertTrue(reader.requests)
        self.assertTrue(all(size == payload.ARCHIVE_READ_CHUNK for size in reader.requests))

    def test_packaged_runtime_stale_marker_is_rejected_before_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle, digest = self.packaged_bundle(directory, stale_marker=True)
            with self.assertRaisesRegex(payload.PayloadError, 'source marker'):
                payload.inspect_bundle(bundle, digest)

    def test_preparation_returns_verified_metadata_and_recheck_detects_changed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle, digest = self.bundle(directory)
            descriptor = Path(directory) / 'bundle.json'
            descriptor.write_text(json.dumps(payload.inspect_bundle(bundle, digest)))
            work = Path(directory) / 'prepared'
            with mock.patch.object(payload, 'validate_generic'), mock.patch.object(payload, 'inspect_bundle', wraps=payload.inspect_bundle) as inspect:
                prepared, metadata = payload.prepare_payload(bundle, descriptor, work)
                self.assertEqual(prepared, work)
                self.assertTrue((prepared / 'rootfs/etc/passwd').is_file())
                payload.verify_prepared_bundle(metadata, bundle)
                self.assertEqual(inspect.call_count, 1)
                with bundle.open('ab') as stream:
                    stream.write(b'changed')
                with self.assertRaises(payload.PayloadError):
                    payload.verify_prepared_bundle(metadata, bundle)

    def test_copy_preserves_metadata_and_separates_boot_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            prepared = base / 'prepared'
            source = prepared / 'rootfs'
            (source / 'boot').mkdir(parents=True)
            (source / 'etc').mkdir()
            config = source / 'etc/config'
            config.write_text('target config\n')
            config.chmod(0o640)
            os.setxattr(config, 'user.installer-test', b'metadata')
            (source / 'etc/config-link').symlink_to('config')
            os.link(config, source / 'etc/config-hardlink')
            (source / 'boot/kernel.img').write_bytes(b'kernel')
            root = base / 'target'
            boot = root / 'boot'
            boot.mkdir(parents=True)
            payload.copy_payload(prepared, root, boot)
            copied = root / 'etc/config'
            self.assertEqual(copied.stat().st_mode & 0o777, 0o640)
            self.assertEqual(os.getxattr(copied, 'user.installer-test'), b'metadata')
            self.assertEqual(copied.stat().st_ino, (root / 'etc/config-hardlink').stat().st_ino)
            self.assertTrue((root / 'etc/config-link').is_symlink())
            self.assertEqual((boot / 'kernel.img').read_bytes(), b'kernel')

    def test_copy_refuses_nonempty_target_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            prepared = base / 'prepared'
            (prepared / 'rootfs/boot').mkdir(parents=True)
            root = base / 'target'
            (root / 'boot').mkdir(parents=True)
            (root / 'keep').write_text('existing')
            with self.assertRaises(payload.PayloadError):
                payload.copy_payload(prepared, root, root / 'boot')
            self.assertEqual((root / 'keep').read_text(), 'existing')


if __name__ == '__main__':
    unittest.main()
