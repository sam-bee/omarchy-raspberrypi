#!/usr/bin/python3
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import desktop_payload as payload


class PayloadTests(unittest.TestCase):
    def bundle(self, directory, *, unsafe=False, symlink_child=False):
        archive = Path(directory) / 'payload.tar'
        manifest = {'schema_version': 1, 'source': {'revision': 'a' * 40},
                    'target': {'architecture': 'aarch64', 'profile': 'rpi5'}}
        with tarfile.open(archive, 'w') as stream:
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
