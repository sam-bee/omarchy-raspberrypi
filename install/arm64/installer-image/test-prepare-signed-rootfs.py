#!/usr/bin/python3
"""Focused, unprivileged tests for prepare-signed-rootfs.py."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("prepare-signed-rootfs.py")
SPEC = importlib.util.spec_from_file_location("prepare_signed_rootfs", MODULE_PATH)
assert SPEC and SPEC.loader
prepare = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = prepare
SPEC.loader.exec_module(prepare)


FINGERPRINT = "68B3537F39A313B3E574D06777193F152BDBE6A6"


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def make_archive(path: Path, *, traversal: bool = False, special: bool = False) -> None:
    with tarfile.open(path, "w") as archive:
        directory = tarfile.TarInfo("etc")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        archive.addfile(directory)

        if traversal:
            entry = tarfile.TarInfo("../outside")
            entry.size = 1
            archive.addfile(entry, io.BytesIO(b"x"))
        elif special:
            entry = tarfile.TarInfo("dev/block")
            entry.type = tarfile.BLKTYPE
            entry.devmajor = 1
            entry.devminor = 7
            archive.addfile(entry)
        else:
            content = b"NAME=omarchy\n"
            entry = tarfile.TarInfo("etc/os-release")
            entry.mode = 0o640
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
            link = tarfile.TarInfo("bin")
            link.type = tarfile.SYMTYPE
            link.linkname = "usr/bin"
            archive.addfile(link)


class PrepareSignedRootfsTests(unittest.TestCase):
    def make_tools(self, directory: Path) -> tuple[Path, Path, Path]:
        gpgv = directory / "gpgv"
        write_executable(
            gpgv,
            "#!/bin/sh\n"
            f"printf '%s\\n' '[GNUPG:] VALIDSIG {FINGERPRINT} 20260927 0 0 1 10 00 {FINGERPRINT}'\n",
        )
        gpg = directory / "gpg"
        marker = directory / "gpg-called"
        write_executable(
            gpg,
            "#!/bin/sh\n"
            "set -eu\n"
            "output=''\n"
            "while [ $# -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    --output) output=$2; shift 2 ;;\n"
            "    --batch|--no-options|--dearmor) shift ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            f"printf '%s' 'binary keyring' > {marker}\n"
            "printf '%s' 'binary keyring' > \"$output\"\n",
        )
        bsdtar = directory / "bsdtar"
        write_executable(
            bsdtar,
            "#!/bin/sh\n"
            "set -eu\n"
            "archive=''\n"
            "destination=''\n"
            "while [ $# -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    -xpf) archive=$2; shift 2 ;;\n"
            "    -C) destination=$2; shift 2 ;;\n"
            "    --numeric-owner) shift ;;\n"
            "    *) echo \"unexpected argument: $1\" >&2; exit 2 ;;\n"
            "  esac\n"
            "done\n"
            "tar -xpf \"$archive\" -C \"$destination\" --preserve-permissions\n",
        )
        return gpgv, gpg, bsdtar

    def make_inputs(self, directory: Path) -> tuple[Path, Path, Path]:
        archive = directory / "ArchLinuxARM-rpi-aarch64.tar.gz"
        make_archive(archive)
        signature = directory / (archive.name + ".sig")
        signature.write_bytes(b"detached signature")
        keyring = directory / "pubring.gpg"
        keyring.write_bytes(b"trusted keyring")
        return archive, signature, keyring

    def test_fingerprint_and_hash_guards(self) -> None:
        self.assertEqual(prepare._validate_fingerprint(FINGERPRINT.lower()), FINGERPRINT)
        with self.assertRaises(prepare.RootfsPreparationError):
            prepare._validate_fingerprint("not-a-fingerprint")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive"
            path.write_bytes(b"archive bytes")
            digest, size = prepare._sha256_file(path)
            self.assertEqual(digest, hashlib.sha256(b"archive bytes").hexdigest())
            self.assertEqual(size, len(b"archive bytes"))

    def test_output_guards_reject_existing_symlink_protected_and_device_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "existing"
            existing.mkdir()
            with self.assertRaises(prepare.RootfsPreparationError):
                prepare.validate_output_directory(existing)
            link = root / "link"
            link.symlink_to(existing, target_is_directory=True)
            with self.assertRaises(prepare.RootfsPreparationError):
                prepare.validate_output_directory(link)
            with self.assertRaises(prepare.RootfsPreparationError):
                prepare.validate_output_directory(Path("/"))
            with self.assertRaises(prepare.RootfsPreparationError):
                prepare.validate_output_directory(Path("/dev/omarchy-rootfs-test"))

    def test_archive_inspection_rejects_traversal_and_special_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            traversal = root / "traversal.tar"
            make_archive(traversal, traversal=True)
            with self.assertRaises(prepare.RootfsPreparationError):
                prepare.inspect_archive(traversal)
            special = root / "special.tar"
            make_archive(special, special=True)
            with self.assertRaises(prepare.RootfsPreparationError):
                prepare.inspect_archive(special)

    def test_prepare_verifies_extracts_and_writes_nonsecret_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, signature, keyring = self.make_inputs(root)
            gpgv, gpg, bsdtar = self.make_tools(root)
            output = root / "prepared"
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            manifest = prepare.prepare_rootfs(
                archive,
                signature,
                keyring,
                FINGERPRINT,
                output,
                expected_archive_sha256=digest,
                gpgv_command=os.fspath(gpgv),
                gpg_command=os.fspath(gpg),
                bsdtar_command=os.fspath(bsdtar),
                require_native_aarch64=False,
                require_root=False,
            )
            extracted = output / "rootfs"
            self.assertEqual((extracted / "etc/os-release").read_bytes(), b"NAME=omarchy\n")
            self.assertTrue((extracted / "bin").is_symlink())
            self.assertEqual(stat.S_IMODE((extracted / "etc/os-release").stat().st_mode), 0o640)
            on_disk = json.loads((output / "rootfs-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(on_disk, manifest)
            self.assertEqual(on_disk["source"]["archive_sha256"], digest)
            self.assertEqual(on_disk["source"]["signer_fingerprint"], FINGERPRINT)
            self.assertEqual(on_disk["source"]["keyring_format"], "binary")
            self.assertEqual(on_disk["source"]["keyring_sha256"], hashlib.sha256(keyring.read_bytes()).hexdigest())
            self.assertNotIn("detached signature", json.dumps(on_disk))
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)

    def test_prepare_rejects_hash_mismatch_before_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, signature, keyring = self.make_inputs(root)
            gpgv, gpg, bsdtar = self.make_tools(root)
            output = root / "prepared"
            with self.assertRaisesRegex(prepare.RootfsPreparationError, "SHA-256"):
                prepare.prepare_rootfs(
                    archive,
                    signature,
                    keyring,
                    FINGERPRINT,
                    output,
                    expected_archive_sha256="0" * 64,
                    gpgv_command=os.fspath(gpgv),
                    gpg_command=os.fspath(gpg),
                    bsdtar_command=os.fspath(bsdtar),
                    require_native_aarch64=False,
                    require_root=False,
                )
            self.assertFalse(output.exists())

    def test_ascii_armored_keyring_is_dearmored_for_gpgv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, signature, keyring = self.make_inputs(root)
            gpgv, gpg, _ = self.make_tools(root)
            keyring.write_text(
                "-----BEGIN PGP PUBLIC KEY BLOCK-----\n"
                "not a real key in this fake-tool test\n"
                "-----END PGP PUBLIC KEY BLOCK-----\n",
                encoding="ascii",
            )
            signer = prepare.verify_detached_signature(
                archive,
                signature,
                keyring,
                FINGERPRINT,
                gpgv_command=os.fspath(gpgv),
                gpg_command=os.fspath(gpg),
            )
            self.assertEqual(signer, FINGERPRINT)
            self.assertEqual((root / "gpg-called").read_text(encoding="ascii"), "binary keyring")

    def test_invalid_ascii_armored_keyring_fails_before_gpgv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, signature, keyring = self.make_inputs(root)
            gpgv, _, _ = self.make_tools(root)
            keyring.write_text(
                "-----BEGIN PGP PUBLIC KEY BLOCK-----\ninvalid\n",
                encoding="ascii",
            )
            invalid_gpg = root / "invalid-gpg"
            write_executable(invalid_gpg, "#!/bin/sh\nexit 2\n")
            with self.assertRaisesRegex(prepare.RootfsPreparationError, "dearmor trusted keyring"):
                prepare.verify_detached_signature(
                    archive,
                    signature,
                    keyring,
                    FINGERPRINT,
                    gpgv_command=os.fspath(gpgv),
                    gpg_command=os.fspath(invalid_gpg),
                )


if __name__ == "__main__":
    unittest.main()
