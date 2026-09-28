#!/usr/bin/python3
import hashlib
import io
import json
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


if __name__ == '__main__':
    unittest.main()
