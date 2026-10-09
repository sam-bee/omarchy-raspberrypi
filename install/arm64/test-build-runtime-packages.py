#!/usr/bin/python3
"""Archive-level tests for build-runtime-packages.py."""

from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import os
import gzip
import posixpath
from pathlib import Path
import stat
import subprocess
import tarfile
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("build-runtime-packages.py")
SPEC = importlib.util.spec_from_file_location("build_runtime_packages", MODULE_PATH)
assert SPEC and SPEC.loader
builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = builder
SPEC.loader.exec_module(builder)

PAYLOAD_MODULE_PATH = MODULE_PATH.parent / "installer-image/desktop_payload.py"
PAYLOAD_SPEC = importlib.util.spec_from_file_location("omarchy_pi_desktop_payload", PAYLOAD_MODULE_PATH)
assert PAYLOAD_SPEC and PAYLOAD_SPEC.loader
payload_decoder = importlib.util.module_from_spec(PAYLOAD_SPEC)
sys.modules[PAYLOAD_SPEC.name] = payload_decoder
PAYLOAD_SPEC.loader.exec_module(payload_decoder)


class RuntimePackageTests(unittest.TestCase):
    def test_mtree_escape_uses_byte_octal_tokens(self) -> None:
        self.assertEqual(
            builder._mtree_escape("a\\b c\té"),
            r"a\134b\040c\011\303\251",
        )

    def make_source(self, root: Path) -> tuple[Path, str]:
        files = {
            "version": "4.0.0.alpha\n",
            "icon.png": "icon fixture\n",
            "bin/omarchy": "#!/bin/bash\nprintf 'omarchy\\n'\n",
            "bin/omarchy-launch-terminal": "#!/bin/bash\nexec foot\n",
            "config/hypr/hyprland.lua": "return {}\n",
            "config/omarchy/shell.json": "{}\n",
            "default/bash/env-bootstrap": "export OMARCHY_PATH=/usr/share/omarchy\n",
            "default/hypr/input.lua": "return {}\n",
            "default/fonts/omarchy/omarchy.ttf": "font fixture\n",
            "default/systemd/user/omarchy-test.service": "[Service]\nExecStart=/usr/bin/true\n",
            "default/libalpm/hooks/10-omarchy-hyprland-reload-pause.hook": (
                "[Trigger]\nOperation = Install\nOperation = Upgrade\nType = Package\n"
                "Target = omarchy-settings\nTarget = omarchy-settings-dev\n\n"
                "[Action]\nWhen = PreTransaction\nDepends = omarchy\n"
                "Exec = /usr/bin/omarchy-hyprland-reload-guard pause\n"
            ),
            "default/libalpm/hooks/90-omarchy-hyprland-reload-resume.hook": (
                "[Trigger]\nOperation = Install\nOperation = Upgrade\nType = Package\n"
                "Target = omarchy-settings\nTarget = omarchy-settings-dev\n\n"
                "[Action]\nWhen = PostTransaction\nDepends = omarchy\n"
                "Exec = /usr/bin/omarchy-hyprland-reload-guard resume\n"
            ),
            "default/limine/limine.conf": "THIS_MUST_NOT_BE_PACKAGED\n",
            "default/chromium/extensions/copy-url/icon.png": "placeholder symlink\n",
            "default/libalpm/hooks/05-auth.hook": "THIS_MUST_NOT_BE_PACKAGED\n",
            "etc/NetworkManager/dispatcher.d/omarchy": "THIS_MUST_NOT_BE_PACKAGED\n",
            "etc/sudoers.d/omarchy": "THIS_MUST_NOT_BE_PACKAGED\n",
            "etc/fastfetch/config.jsonc": "{}\n",
            "etc/profile.d/omarchy.sh": "export OMARCHY_PATH=/usr/share/omarchy\n",
            "applications/terminal.desktop": "[Desktop Entry]\nName=Terminal\n",
            "applications/Disk Usage.desktop": "[Desktop Entry]\nName=Disk Usage\nIcon=disk-usage\n",
            "applications/icons/terminal.png": "not-a-real-png\n",
            "applications/icons/Disk Usage.png": "not-a-real-png\n",
            "shell/Ui/Main.qml": "Item {}\n",
            "install/user/all.sh": "echo user\n",
            "migrations/1.sh": "echo migrate\n",
            "themes/test/colors.toml": "accent = '#fff'\n",
            "install/arm64/session/90-omarchy-pi": "export OMARCHY_PATH=/usr/share/omarchy\n",
            "install/arm64/session/chromium-flags.conf": "--ozone-platform=wayland\n",
            "install/arm64/session/portals.conf": "default=gtk\n",
            "install/arm64/session/xdg-terminals.list": "x-scheme-handler/terminal=foot.desktop\n",
            "install/arm64/session/fresh-hyprland-prefix.lua": "_G.omarchy_autostart_minimal = true\n",
            "install/arm64/session/ensure-headless-output.sh": "#!/bin/bash\ntrue\n",
            "install/arm64/session/omarchy-lock-password": "THIS_MUST_NOT_BE_PACKAGED\n",
            "install/arm64/session/systemd/omarchy-pi-hypr-rdp.service": "[Service]\nExecStartPre=/usr/bin/python3 /usr/local/libexec/omarchy-pi/verify-hypr-rdp-runtime.py\nExecStart=/usr/bin/true\n",
            "install/arm64/session/systemd/omarchy-pi-uwsm-session@.service": "[Service]\nExecStart=/usr/local/libexec/omarchy-pi/start-uwsm-session.sh %i\n",
            "install/arm64/session/systemd/start-uwsm-session.sh": "#!/bin/bash\ntrue\n",
            "install/arm64/session/systemd/verify-hypr-rdp-runtime.py": "#!/usr/bin/python3\npass\n",
        }
        for relative, content in files.items():
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            if relative.startswith(("bin/", "install/", "migrations/")):
                destination.chmod(0o755)
        symlink = root / "default/chromium/extensions/copy-url/icon.png"
        symlink.unlink()
        symlink.symlink_to("../../../../icon.png")
        subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=root, check=True)
        environment = {
            "GIT_AUTHOR_NAME": "Archive Test",
            "GIT_AUTHOR_EMAIL": "archive-test@example.invalid",
            "GIT_COMMITTER_NAME": "Archive Test",
            "GIT_COMMITTER_EMAIL": "archive-test@example.invalid",
        }
        subprocess.run(["git", "add", "."], cwd=root, check=True, env={**__import__("os").environ, **environment})
        subprocess.run(["git", "commit", "--quiet", "-m", "fixture"], cwd=root, check=True, env={**__import__("os").environ, **environment})
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        return root, revision

    def test_builds_native_archives_with_split_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            source, revision = self.make_source(root)
            output = Path(temporary) / "bundle"
            result = builder.build_bundle(source, output, source_revision=revision)
            self.assertEqual(result["architecture"], "aarch64")
            self.assertEqual(result["source_revision"], revision)
            self.assertEqual([row["package"] for row in result["packages"]], list(builder.PACKAGE_NAMES))
            self.assertTrue((output / "pair.json").is_file())
            self.assertTrue((output / "manifest.tsv").is_file())
            settings, runtime = (output / name for name in (
                f"omarchy-settings-4.0.0.alpha-1-aarch64.pkg.tar.zst",
                f"omarchy-4.0.0.alpha-1-aarch64.pkg.tar.zst",
            ))
            self.assertTrue(settings.is_file())
            self.assertTrue(runtime.is_file())
            # Exercise the same ownership decoder used at the desktop payload
            # input boundary.  The real source has filenames containing
            # spaces (for example ``Disk Usage.desktop``); mtree octal
            # escaping must round-trip those names to the tar members.
            package_metadata, package_owned = payload_decoder._package_contents(settings.read_bytes())
            self.assertEqual(package_metadata["pkgname"], "omarchy-settings")
            self.assertIn("etc/skel/.local/share/applications/Disk Usage.desktop", package_owned)
            settings_names = {member.name for member in builder._archive_members(settings)}
            runtime_names = {member.name for member in builder._archive_members(runtime)}
            self.assertIn("etc/skel/.config/hypr/hyprland.lua", settings_names)
            self.assertEqual(
                builder._archive_member_bytes(
                    settings, "etc/skel/.config/hypr/hyprland.lua"
                ).decode(),
                "_G.omarchy_autostart_minimal = true\nreturn {}\n",
            )
            self.assertIn("usr/share/omarchy/default/bash/env-bootstrap", settings_names)
            self.assertIn("usr/share/omarchy/default/fonts/omarchy/omarchy.ttf", settings_names)
            self.assertIn("usr/share/omarchy/default/systemd/user/omarchy-test.service", settings_names)
            self.assertIn("usr/bin/omarchy", runtime_names)
            self.assertIn("usr/share/omarchy/shell/Ui/Main.qml", runtime_names)
            for size in ("48x48", "256x256", "scalable"):
                self.assertIn(f"usr/share/icons/hicolor/{size}/apps/terminal.png", settings_names)
                self.assertIn(f"usr/share/icons/hicolor/{size}/apps/disk-usage.png", settings_names)
                self.assertNotIn(f"usr/share/icons/hicolor/{size}/apps/Disk Usage.png", settings_names)
            self.assertIn("usr/share/omarchy/.omarchy-pi-source-commit", runtime_names)
            self.assertIn("usr/share/omarchy/.omarchy-pi-packaged.json", runtime_names)
            packaged_marker = json.loads(
                builder._archive_member_bytes(
                    runtime, "usr/share/omarchy/.omarchy-pi-packaged.json"
                ).decode()
            )
            self.assertEqual(packaged_marker["runtime_mode"], "packaged")
            self.assertIn("usr/lib/systemd/system/omarchy-pi-uwsm-session@.service", settings_names)
            self.assertIn("usr/libexec/omarchy-pi/start-uwsm-session.sh", settings_names)
            self.assertIn("etc/skel/.config/uwsm/env.d/90-omarchy-pi", settings_names)
            for source_rel, destination_rel in builder.SAFE_OMARCHY_HOOKS.items():
                self.assertIn(destination_rel, runtime_names)
                self.assertNotIn(destination_rel, settings_names)
                self.assertEqual(
                    builder._archive_member_bytes(runtime, destination_rel),
                    (source / source_rel).read_bytes(),
                )
                hook = builder._archive_member_bytes(runtime, destination_rel).decode()
                self.assertIn("Target = omarchy-settings", hook)
                self.assertIn("Target = omarchy-settings-dev", hook)
                self.assertIn("Depends = omarchy", hook)
            self.assertNotIn("usr/share/libalpm/hooks/05-auth.hook", runtime_names)
            self.assertIn("etc/skel/.config/chromium-flags.conf", settings_names)
            self.assertIn("etc/skel/.config/xdg-desktop-portal/portals.conf", settings_names)
            self.assertIn("etc/skel/.config/xdg-terminals.list", settings_names)
            self.assertEqual(
                builder._archive_member_bytes(settings, "etc/skel/.config/chromium-flags.conf").decode(),
                "--ozone-platform=wayland\n",
            )
            self.assertEqual(
                builder._archive_member_bytes(settings, "etc/skel/.config/xdg-desktop-portal/portals.conf").decode(),
                "default=gtk\n",
            )
            self.assertEqual(
                builder._archive_member_bytes(settings, "etc/skel/.config/xdg-terminals.list").decode(),
                "x-scheme-handler/terminal=foot.desktop\n",
            )
            self.assertEqual(
                builder._archive_member_bytes(
                    settings, "etc/skel/.config/uwsm/env.d/90-omarchy-pi"
                ).decode(),
                builder.PACKAGED_RUNTIME_ENV,
            )
            packaged_hypr_service = builder._archive_member_bytes(
                settings, "usr/lib/systemd/user/omarchy-pi-hypr-rdp.service"
            ).decode()
            packaged_uwsm_service = builder._archive_member_bytes(
                settings, "usr/lib/systemd/system/omarchy-pi-uwsm-session@.service"
            ).decode()
            for service in (packaged_hypr_service, packaged_uwsm_service):
                self.assertIn("/usr/libexec/omarchy-pi/", service)
                self.assertNotIn("/usr/local/libexec/omarchy-pi/", service)
            legacy_hypr_service = (
                builder.HERE / "session/systemd/omarchy-pi-hypr-rdp.service"
            ).read_text(encoding="utf-8")
            legacy_uwsm_service = (
                builder.HERE / "session/systemd/omarchy-pi-uwsm-session@.service"
            ).read_text(encoding="utf-8")
            self.assertIn("/usr/local/libexec/omarchy-pi/", legacy_hypr_service)
            self.assertIn("/usr/local/libexec/omarchy-pi/", legacy_uwsm_service)
            self.assertNotIn("usr/share/omarchy/default/limine/limine.conf", settings_names)
            self.assertNotIn("etc/NetworkManager/dispatcher.d/omarchy", settings_names)
            self.assertNotIn("etc/sudoers.d/omarchy", settings_names)
            self.assertNotIn("default/libalpm/hooks/05-auth.hook", settings_names)
            self.assertNotIn("etc/pam.d/omarchy-lock-password", settings_names)
            self.assertNotIn("usr/share/omarchy/install/arm64/session/omarchy-lock-password", runtime_names)
            # Every directory emitted into the tar must also be present in
            # native pacman's .MTREE.  Pacman uses this inventory when it
            # writes its installed file list; omitting dirs produces one
            # warning per parent directory during -U.
            mtree_lines = gzip.decompress(
                builder._archive_member_bytes(settings, ".MTREE")
            ).decode().splitlines()
            mtree_entries = {
                line.split(" ", 1)[0]
                for line in mtree_lines
                if line.startswith(".")
            }
            for member in builder._archive_members(settings):
                if member.isdir():
                    expected = "." if member.name == "." else f"./{builder._mtree_escape(member.name)}"
                    self.assertIn(expected, mtree_entries, member.name)
            # Compare every non-metadata tar member with its native mtree
            # record, including uid/gid, mode, timestamp, size, digest, and
            # symlink target.  Directory names alone are insufficient for
            # pacman -Qkk to establish archive integrity.
            for archive_path in (settings, runtime):
                raw_archive = subprocess.run(
                    ["zstd", "-q", "-d", "-c", os.fspath(archive_path)],
                    check=True,
                    capture_output=True,
                ).stdout
                with tarfile.open(fileobj=io.BytesIO(raw_archive), mode="r:") as archive_stream:
                    archive_mtree = gzip.decompress(
                        archive_stream.extractfile(".MTREE").read()
                    ).decode().splitlines()
                    mtree_records: dict[str, dict[str, str]] = {}
                    for line in archive_mtree:
                        if line.startswith("."):
                            fields = line.split()
                            encoded_path = fields[0]
                            path = "." if encoded_path == "." else payload_decoder._decode_mtree_path(encoded_path[2:])
                            mtree_records[path] = {
                                key: value
                                for key, value in (field.split("=", 1) for field in fields[1:] if "=" in field)
                            }
                    for member in archive_stream:
                        if member.name.startswith(".") and member.name != ".":
                            continue
                        record = mtree_records.get(member.name)
                        self.assertIsNotNone(record, f"{archive_path.name}: {member.name}")
                        assert record is not None
                        expected_type = "dir" if member.isdir() else "link" if member.issym() else "file"
                        self.assertEqual(record.get("type"), expected_type, member.name)
                        self.assertEqual(record.get("uid"), "0", member.name)
                        self.assertEqual(record.get("gid"), "0", member.name)
                        self.assertEqual(int(record["mode"], 8), stat.S_IMODE(member.mode), member.name)
                        self.assertEqual(record.get("time"), "0", member.name)
                        if member.isreg():
                            data = archive_stream.extractfile(member)
                            self.assertIsNotNone(data, member.name)
                            content = data.read() if data is not None else b""
                            self.assertEqual(int(record["size"]), len(content), member.name)
                            self.assertEqual(record["sha256digest"], hashlib.sha256(content).hexdigest(), member.name)
                        elif member.issym():
                            self.assertEqual(
                                payload_decoder._decode_mtree_path(record["link"]),
                                member.linkname,
                                member.name,
                            )
            # Source-relative links in defaults must resolve within the same
            # package tree.  This catches a relocated icon becoming dangling.
            settings_members = builder._archive_members(settings)
            settings_names = {member.name for member in settings_members}
            for member in builder._archive_members(settings) + builder._archive_members(runtime):
                if member.isreg():
                    self.assertEqual((member.uid, member.gid), (0, 0), member.name)
                    self.assertFalse(stat.S_IMODE(member.mode) & 0o022, member.name)
            for member in settings_members:
                if member.issym():
                    resolved = posixpath.normpath(
                        posixpath.join(posixpath.dirname(member.name), member.linkname)
                    )
                    self.assertIn(resolved, settings_names, member.name)
            copy_url_link = next(
                member for member in settings_members
                if member.name == "usr/share/omarchy/default/chromium/extensions/copy-url/icon.png"
            )
            self.assertTrue(copy_url_link.issym())
            self.assertIn("usr/share/omarchy/icon.png", settings_names)
            pair = json.loads((output / "pair.json").read_text(encoding="utf-8"))
            self.assertEqual(pair["version"], "4.0.0.alpha-1")
            self.assertEqual(pair["channel"], "stable")
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual([record["name"] for record in manifest["packages"]], list(builder.PACKAGE_NAMES))
            self.assertTrue(all(record["filename"].endswith(".pkg.tar.zst") for record in manifest["packages"]))
            self.assertEqual(builder.check_bundle(output)["version"], "4.0.0.alpha-1")

    def test_check_rejects_archive_pair_with_changed_native_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            source, revision = self.make_source(root)
            output = Path(temporary) / "bundle"
            builder.build_bundle(source, output, source_revision=revision)
            pair = json.loads((output / "pair.json").read_text(encoding="utf-8"))
            pair["version"] = "9.9.9-1"
            (output / "pair.json").write_text(json.dumps(pair), encoding="utf-8")
            with self.assertRaisesRegex(builder.RuntimePackageError, "pair.json does not match"):
                builder.check_bundle(output)

    def test_caller_provided_signatures_are_carried_and_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            source, revision = self.make_source(root)
            signatures = Path(temporary) / "signatures"
            signatures.mkdir()
            for name in builder.PACKAGE_NAMES:
                archive = f"{name}-4.0.0.alpha-1-aarch64.pkg.tar.zst"
                (signatures / (archive + ".sig")).write_bytes(b"caller supplied detached signature\n")
            output = Path(temporary) / "bundle"
            builder.build_bundle(source, output, source_revision=revision, signature_dir=signatures)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual({record["package_signature"] for record in manifest["packages"]}, {"provided"})
            self.assertTrue(all(record["signature"] for record in manifest["packages"]))
            builder.check_bundle(output)

    def test_default_release_tracks_pinned_commit_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            source, revision = self.make_source(root)
            (source / "second-source-file").write_text("second\n", encoding="utf-8")
            environment = {
                "GIT_AUTHOR_NAME": "Archive Test",
                "GIT_AUTHOR_EMAIL": "archive-test@example.invalid",
                "GIT_COMMITTER_NAME": "Archive Test",
                "GIT_COMMITTER_EMAIL": "archive-test@example.invalid",
            }
            subprocess.run(["git", "add", "."], cwd=source, check=True, env={**os.environ, **environment})
            subprocess.run(["git", "commit", "--quiet", "-m", "second fixture"], cwd=source, check=True, env={**os.environ, **environment})
            revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
            result = builder.build_bundle(source, Path(temporary) / "bundle", source_revision=revision)
            self.assertEqual(result["version"], "4.0.0.alpha-2")

    def test_dirty_source_and_source_digest_mismatch_fail_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            source, revision = self.make_source(root)
            (source / "bin/omarchy").write_text("changed\n", encoding="utf-8")
            output = Path(temporary) / "bundle"
            with self.assertRaisesRegex(builder.RuntimePackageError, "must be clean"):
                builder.build_bundle(source, output, source_revision=revision)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
