#!/usr/bin/python3
"""Focused tests for mounted-target provisioning boundaries."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("installed_target", HERE / "installed_target.py")
assert SPEC and SPEC.loader
installed = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installed
SPEC.loader.exec_module(installed)

RDP_SPEC = importlib.util.spec_from_file_location(
    "verify_hypr_rdp_runtime",
    HERE.parent / "session/systemd/verify-hypr-rdp-runtime.py",
)
assert RDP_SPEC and RDP_SPEC.loader
rdp_runtime = importlib.util.module_from_spec(RDP_SPEC)
sys.modules[RDP_SPEC.name] = rdp_runtime
RDP_SPEC.loader.exec_module(rdp_runtime)


PUBLIC_KEY = "ssh-ed25519 " + ("A" * 43) + "="


class FakeRunner:
    def __init__(self, root: Path, boot: Path) -> None:
        self.root = root
        self.boot = boot
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []

    def __call__(
        self,
        command: list[str],
        *,
        input: str | None,
        text: bool,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        self.inputs.append(input)
        if command and command[0] == "/bin/bash" and "--rootfs" in command:
            # The real leaf uses useradd --root.  The fixture simulates its
            # result and intentionally has no host account utility at all.
            passwd = self.root / "etc/passwd"
            passwd.write_text(
                passwd.read_text(encoding="utf-8")
                + "desk:x:1001:1001:Desktop:/home/desk:/bin/bash\n",
                encoding="utf-8",
            )
            home = self.root / "home/desk"
            if "--runtime-layout" in command:
                (self.root / "usr/share/omarchy/install/arm64").mkdir(parents=True, exist_ok=True)
                (self.root / "usr/share/omarchy/.omarchy-pi-source-commit").write_text("a" * 40 + "\n")
                (self.root / "usr/share/omarchy/.omarchy-pi-packaged.json").write_text(
                    json.dumps({"runtime_mode": "packaged", "source_revision": "a" * 40}),
                    encoding="ascii",
                )
                validator = self.root / "usr/libexec/omarchy-pi/verify-hypr-rdp-runtime.py"
                validator.parent.mkdir(parents=True, exist_ok=True)
                validator.write_text(
                    installed._runtime_validator_bytes().decode("utf-8"),
                    encoding="utf-8",
                )
                validator.chmod(0o755)
                setup = self.root / "usr/share/omarchy/install/arm64/setup-desktop-user.sh"
                setup.write_text("#!/bin/bash\n", encoding="ascii")
                setup.chmod(0o755)
                for vendor in (
                    self.root / "usr/lib/systemd/system/omarchy-pi-uwsm-session@.service",
                    self.root / "usr/lib/systemd/user/omarchy-pi-hypr-rdp.service",
                ):
                    vendor.parent.mkdir(parents=True, exist_ok=True)
                    vendor.write_text("[Unit]\n", encoding="ascii")
                env = home / ".config/uwsm/env.d/90-omarchy-pi"
                env.parent.mkdir(parents=True, exist_ok=True)
                env.write_text('export OMARCHY_PATH="/usr/share/omarchy"\n', encoding="ascii")
            else:
                (home / ".local/share/omarchy-pi/current/install/arm64").mkdir(parents=True)
            os.chown(home, os.getuid(), os.getgid())
            wants = self.root / "etc/systemd/system/multi-user.target.wants/omarchy-pi-uwsm-session@desk.service"
            wants.parent.mkdir(parents=True, exist_ok=True)
            if "--runtime-layout" in command:
                wants.symlink_to("/usr/lib/systemd/system/omarchy-pi-uwsm-session@.service")
            else:
                wants.symlink_to("../omarchy-pi-uwsm-session@.service")
            return subprocess.CompletedProcess(command, 0, "", "")

        if command and command[0] == "systemctl":
            self._assert_no_daemon_start(command)
            wants = self.root / "etc/systemd/system/multi-user.target.wants/sshd.service"
            wants.parent.mkdir(parents=True, exist_ok=True)
            if not wants.exists() and not wants.is_symlink():
                wants.symlink_to("/usr/lib/systemd/system/sshd.service")
            return subprocess.CompletedProcess(command, 0, "", "")

        if command and command[0] == "systemd-nspawn":
            self._assert_nspawn_target(command)
            inner = command[command.index("--") + 1 :]
            if inner[:2] == ["/usr/bin/locale", "-a"]:
                return subprocess.CompletedProcess(command, 0, "C.UTF-8\n", "")
            if inner[:2] == ["/usr/bin/localectl", "list-keymaps"]:
                return subprocess.CompletedProcess(command, 0, "gb\n", "")
            if inner[:2] == ["/usr/bin/ssh-keygen", "-A"]:
                ssh = self.root / "etc/ssh"
                ssh.mkdir(parents=True, exist_ok=True)
                (ssh / "ssh_host_ed25519_key").write_bytes(b"private")
                (ssh / "ssh_host_ed25519_key.pub").write_text(PUBLIC_KEY + " fixture\n", encoding="ascii")
                (ssh / "ssh_host_ed25519_key").chmod(0o600)
                (ssh / "ssh_host_ed25519_key.pub").chmod(0o644)
            if inner and inner[0] == "/usr/bin/chpasswd":
                self.assert_secret_only_on_stdin(input)
                shadow = self.root / "etc/shadow"
                rows = []
                for line in shadow.read_text(encoding="utf-8").splitlines():
                    if line.startswith("desk:"):
                        rows.append("desk:$6$fixture$active-password-hash:::::::")
                    else:
                        rows.append(line)
                shadow.write_text("\n".join(rows) + "\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        if command and command[0] in {"useradd", "usermod", "chpasswd", "ssh-keygen"}:
            raise AssertionError("host account or host identity command was invoked")
        return subprocess.CompletedProcess(command, 0, "", "")

    def assert_secret_only_on_stdin(self, value: str | None) -> None:
        assert value is not None and value.startswith("desk:")
        assert "target-login-secret" in value

    def _assert_nspawn_target(self, command: list[str]) -> None:
        assert "--network-namespace-path=/proc/1/ns/net" in command
        assert "--resolv-conf=replace-host" in command
        assert "--timezone=off" in command
        assert "--pipe" in command
        assert f"--bind={self.boot}:/boot" in command
        assert command[command.index("--directory") + 1] == str(self.root)
        assert command.index("--") > command.index("--directory")

    @staticmethod
    def _assert_no_daemon_start(command: list[str]) -> None:
        assert "start" not in command and "--now" not in command


class InstalledTargetTests(unittest.TestCase):
    def assert_readable_as_uid(self, path: Path, uid: int) -> None:
        """Exercise the public target path with the selected non-root UID."""

        if os.geteuid() == uid:
            self.assertTrue(path.read_bytes())
            return
        if os.geteuid() != 0:
            self.skipTest("dropping to the fixture account requires root")
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            try:
                os.setgroups([])
                os.setgid(uid)
                os.setuid(uid)
                os.write(write_fd, b"ok:" + path.read_bytes())
                status = 0
            except BaseException as exc:  # pragma: no cover - reported by parent
                os.write(write_fd, f"error: {exc!r}".encode("utf-8", "replace"))
                status = 1
            finally:
                os.close(write_fd)
            os._exit(status)
        os.close(write_fd)
        result = os.read(read_fd, 64 * 1024)
        os.close(read_fd)
        _, status = os.waitpid(child, 0)
        self.assertEqual(status, 0, result.decode("utf-8", "replace"))
        self.assertTrue(result.startswith(b"ok:"), result)

    def test_validate_settings_rejects_unknown_fields_and_secret_echo(self) -> None:
        settings = {
            "username": "desk",
            "hostname": "pi-target",
            "password": "target-login-secret",
            "timezone": "Europe/London",
            "locale": "C.UTF-8",
            "keymap": "gb",
            "wifi": None,
            "ssh_enabled": True,
            "ssh_authorized_key": PUBLIC_KEY,
            "rdp_mode": "disabled",
            "rdp_password": None,
            "encryption": "plain",
            "recovery_passphrase": None,
        }
        validated = installed.validate_settings(settings)
        self.assertEqual(validated["username"], "desk")
        with self.assertRaises(installed.TargetProvisionError) as context:
            installed.validate_settings({**settings, "unexpected": "target-login-secret"})
        self.assertNotIn("target-login-secret", str(context.exception))
        for field in settings:
            with self.subTest(missing=field), self.assertRaises(installed.TargetProvisionError):
                installed.validate_settings({key: value for key, value in settings.items() if key != field})

    def test_validate_target_options_is_read_only_and_checks_target_sources(self) -> None:
        temporary, root, boot, payload, settings, storage = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
        validated = installed.validate_target_options(root, settings)
        after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
        self.assertEqual(validated["timezone"], "Europe/London")
        self.assertEqual(before, after)
        with self.assertRaises(installed.TargetProvisionError):
            installed.validate_target_options(root, {**settings, "keymap": "missing"})

    def test_boot_runner_keeps_mkinitcpio_nspawn_on_pipes(self) -> None:
        temporary, root, boot, payload, settings, storage = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        calls: list[list[str]] = []

        def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        boot_runner = installed._boot_runner(runner, root, boot)
        boot_runner(
            [
                "systemd-nspawn",
                "-D",
                str(root),
                "--register=no",
                "--private-network",
                "/usr/bin/mkinitcpio",
                "-P",
            ],
            check=False,
        )
        self.assertEqual(len(calls), 1)
        transformed = calls[0]
        self.assertIn("--pipe", transformed)
        self.assertNotIn("--private-network", transformed)
        self.assertIn("--network-namespace-path=/proc/1/ns/net", transformed)
        self.assertIn("--resolv-conf=replace-host", transformed)
        self.assertIn("--timezone=off", transformed)
        self.assertIn(f"--bind={boot}:/boot", transformed)

    def test_storage_accepts_fat_serial_uuid_and_rejects_nonfat_forms(self) -> None:
        temporary, root, boot, payload, settings, storage = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        storage = {**storage, "boot_uuid": "ABCD-1234"}
        checked = installed._validate_storage(storage, installed.validate_settings(settings))
        self.assertEqual(checked["boot_uuid"], "ABCD-1234")
        installed._write_fstab(root, checked)
        self.assertIn("UUID=ABCD-1234 /boot vfat", (root / "etc/fstab").read_text(encoding="utf-8"))
        for invalid in ("abcd1234", "ABCD1234", "0000-0000", "ABCD-1234-5678"):
            with self.subTest(invalid=invalid), self.assertRaises(installed.TargetProvisionError):
                installed._validate_storage({**storage, "boot_uuid": invalid}, installed.validate_settings(settings))

    def test_wifi_profile_persists_target_country_without_host_network_commands(self) -> None:
        temporary, root, boot, payload, settings, storage = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        validated = installed.validate_settings(
            {**settings, "wifi": {"country": "GB", "ssid": "target-wifi", "password": "wifi-secret"}}
        )
        installed._configure_network(root, validated)
        profile = root / "etc/NetworkManager/system-connections/20-omarchy-pi-target-wifi.nmconnection"
        self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
        text = profile.read_text(encoding="utf-8")
        self.assertIn("ssid=target-wifi", text)
        self.assertIn("psk=wifi-secret", text)
        self.assertIn("powersave=2", text)
        self.assertIn("ieee80211_regdom=GB", (root / "etc/modprobe.d/omarchy-pi-regdom.conf").read_text())

    def test_ssh_key_directory_chown_targets_fixture_account_when_worker_is_root(self) -> None:
        temporary, root, boot, payload, settings, storage = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        home = root / "home/desk"
        home.mkdir(parents=True)
        account = installed.Account("desk", 2001, 2002, home)
        runner = FakeRunner(root, boot)
        with patch.object(installed.os, "geteuid", return_value=0), patch.object(installed.os, "fchown"), patch.object(installed.os, "chown") as chown:
            installed._configure_ssh(root, boot, account, installed.validate_settings(settings), runner=runner)
        ssh_directory = home / ".ssh"
        chown.assert_any_call(ssh_directory, account.uid, account.gid)
        self.assertEqual(stat.S_IMODE(ssh_directory.stat().st_mode), 0o700)

    def make_fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Path, Path, Path, dict, dict]:
        temporary = tempfile.TemporaryDirectory(prefix="omarchy-installed-target-")
        base = Path(temporary.name)
        root = base / "target"
        boot = root / "boot"
        payload = base / "payload"
        source = payload / "source"
        for directory in (
            root / "etc/ssh",
            root / "etc/systemd/system/multi-user.target.wants",
            root / "etc/sudoers.d",
            root / "etc/NetworkManager/system-connections",
            root / "usr/share/zoneinfo/Europe",
            root / "usr/share/i18n/locales",
            root / "usr/share/kbd/keymaps",
            root / "usr/bin",
            root / "usr/local/libexec/omarchy-pi",
            boot,
            source / "install/arm64",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        (root / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/bash\n", encoding="utf-8")
        (root / "etc/group").write_text("root:x:0:\n", encoding="utf-8")
        (root / "etc/shadow").write_text("root:!:1::::::\ndesk:!:1::::::\n", encoding="utf-8")
        (root / "usr/share/zoneinfo/Europe/London").write_bytes(b"tz")
        (root / "usr/share/i18n/locales/C").write_bytes(b"locale")
        (root / "usr/share/kbd/keymaps/gb.map.gz").write_bytes(b"keymap")
        validator = root / "usr/local/libexec/omarchy-pi/verify-hypr-rdp-runtime.py"
        validator.write_text("PROFILE_POLICY = True\ndef check_profile_policy(): pass\n", encoding="ascii")
        validator.chmod(0o755)
        (source / "install/arm64/provision-desktop-root.sh").write_text("#!/bin/bash\n", encoding="ascii")
        storage = {
            "root": root,
            "boot": boot,
            "root_uuid": "11111111-1111-1111-1111-111111111111",
            "boot_uuid": "ABCD-1234",
            "luks_uuid": None,
            "key_uuid": None,
            "key_path": None,
        }
        settings = {
            "username": "desk",
            "hostname": "pi-target",
            "password": "target-login-secret",
            "timezone": "Europe/London",
            "locale": "C.UTF-8",
            "keymap": "gb",
            "wifi": None,
            "ssh_enabled": True,
            "ssh_authorized_key": PUBLIC_KEY,
            "rdp_mode": "loopback",
            "rdp_password": "target-rdp-secret",
            "encryption": "plain",
            "recovery_passphrase": None,
        }
        return temporary, root, boot, payload, settings, storage

    def test_provision_uses_target_context_and_keeps_secrets_out_of_summary(self) -> None:
        temporary, root, boot, payload, settings, storage = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        provenance_path = root / "var/lib/omarchy-pi/desktop-user-provision.json"
        provenance_path.parent.mkdir(parents=True)
        provenance_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_revision": "d" * 40,
                    "target_user": "desk",
                    "target_home": "/home/desk",
                    "package_manifest_sha256": "",
                    "identity_policy": "generate machine and account identities on target installation",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        provenance = {
            "installer": {"source_revision": "a" * 40, "runtime_sha256": "b" * 64},
            "desktop_bundle_sha256": "c" * 64,
        }
        runner = FakeRunner(root, boot)
        original = installed._configure_boot
        installed._configure_boot = lambda *args, **kwargs: None
        self.addCleanup(lambda: setattr(installed, "_configure_boot", original))
        progress: list[str] = []

        summary = installed.provision_target(
            root,
            payload,
            settings,
            storage,
            progress.append,
            runner=runner,
            machine="aarch64",
            provenance=provenance,
        )

        self.assertEqual(summary["username"], "desk")
        self.assertEqual(summary["rdp_bind"], "127.0.0.1:3389")
        rdp_config = (root / "home/desk/.config/omarchy-pi-rdp/config.toml").read_text(encoding="utf-8")
        self.assertIn('password_file = "/home/desk/.config/omarchy-pi-rdp/password"', rdp_config)
        self.assertNotIn("resolution =", rdp_config)
        self.assertNotIn(str(root), rdp_config)
        self.assertNotIn("target-login-secret", repr(summary))
        self.assertNotIn("target-rdp-secret", repr(summary))
        self.assertNotIn("target-login-secret", " ".join(progress))
        self.assertTrue(any(call[0] == "systemd-nspawn" for call in runner.calls))
        self.assertTrue(any(value and "target-login-secret" in value for value in runner.inputs))
        for call in runner.calls:
            self.assertNotIn("target-login-secret", call)
            self.assertNotIn("target-rdp-secret", call)
        self.assertIn("UUID=11111111-1111-1111-1111-111111111111", (root / "etc/fstab").read_text())
        self.assertIn("root=UUID=11111111-1111-1111-1111-111111111111", (boot / "cmdline.txt").read_text())
        self.assertEqual((root / "etc/hostname").read_text(), "pi-target\n")
        localtime = root / "etc/localtime"
        self.assertTrue(localtime.is_symlink())
        self.assertEqual(os.readlink(localtime), "/usr/share/zoneinfo/Europe/London")
        ssh_directory = root / "home/desk/.ssh"
        self.assertTrue((ssh_directory / "authorized_keys").exists())
        account = installed._account_from_target(root, "desk")
        self.assertEqual(ssh_directory.stat().st_uid, account.uid)
        self.assertEqual(ssh_directory.stat().st_gid, account.gid)
        self.assertEqual(stat.S_IMODE(ssh_directory.stat().st_mode), 0o700)
        self.assert_readable_as_uid(ssh_directory / "authorized_keys", account.uid)
        receipt = json.loads(provenance_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["source_revision"], "d" * 40)
        self.assertEqual(receipt["installer"], provenance["installer"])
        self.assertEqual(receipt["desktop_bundle_sha256"], provenance["desktop_bundle_sha256"])
        self.assertIn("provenance", progress)

        account = installed._account_from_target(root, "desk")
        validated = installed.validate_settings(settings)
        checked_storage = installed._validate_storage(storage, validated)
        for replacement in ("/usr/share/zoneinfo/UTC", None):
            with self.subTest(replacement=replacement):
                if localtime.is_symlink() or localtime.exists():
                    localtime.unlink()
                if replacement is not None:
                    localtime.symlink_to(replacement)
        with self.assertRaisesRegex(installed.TargetProvisionError, "timezone link"):
                    installed._validate_result(root, account, validated, checked_storage, summary["rdp_bind"])

    def test_packaged_runtime_uses_system_path_and_skips_release_pointer(self) -> None:
        temporary, root, boot, payload, settings, storage = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        source = payload / "source"
        subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=source, check=True)
        environment = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Installed target test",
            "GIT_AUTHOR_EMAIL": "installed-target@example.invalid",
            "GIT_COMMITTER_NAME": "Installed target test",
            "GIT_COMMITTER_EMAIL": "installed-target@example.invalid",
        }
        subprocess.run(["git", "add", "."], cwd=source, check=True, env=environment)
        subprocess.run(["git", "commit", "--quiet", "-m", "fixture"], cwd=source, check=True, env=environment)
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        (payload / "desktop-manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": {"revision": revision},
                    "runtime": {
                        "layout": "packaged",
                        "path": "/usr/share/omarchy",
                        "source_revision": revision,
                    },
                }
            ),
            encoding="utf-8",
        )
        # FakeRunner's package marker is deliberately fixed to the test's
        # expected source value; the target leaf only needs a package-owned
        # marker and helper for this integration boundary.
        runner = FakeRunner(root, boot)
        original = installed._configure_boot
        installed._configure_boot = lambda *args, **kwargs: None
        self.addCleanup(lambda: setattr(installed, "_configure_boot", original))
        summary = installed.provision_target(
            root,
            payload,
            settings,
            storage,
            lambda phase: None,
            runner=runner,
            machine="aarch64",
        )
        self.assertEqual(summary["username"], "desk")
        provision_calls = [call for call in runner.calls if call and call[0] == "/bin/bash" and "--rootfs" in call]
        self.assertEqual(len(provision_calls), 1)
        self.assertIn("--runtime-layout", provision_calls[0])
        setup_calls = [call for call in runner.calls if "/usr/share/omarchy/install/arm64/setup-desktop-user.sh" in call]
        self.assertEqual(len(setup_calls), 1)
        self.assertFalse((root / "home/desk/.local/share/omarchy-pi/current").exists())
        self.assertIn('export OMARCHY_PATH="/usr/share/omarchy"', (root / "home/desk/.config/uwsm/env.d/90-omarchy-pi").read_text())
        rdp_wants = root / "home/desk/.config/systemd/user/graphical-session.target.wants/omarchy-pi-hypr-rdp.service"
        self.assertEqual(os.readlink(rdp_wants), "/usr/lib/systemd/user/omarchy-pi-hypr-rdp.service")
        session_wants = root / "etc/systemd/system/multi-user.target.wants/omarchy-pi-uwsm-session@desk.service"
        self.assertEqual(os.readlink(session_wants), "/usr/lib/systemd/system/omarchy-pi-uwsm-session@.service")
        self.assertFalse((root / "etc/systemd/system/omarchy-pi-uwsm-session@.service").exists())
        self.assertFalse((root / "etc/systemd/user/omarchy-pi-hypr-rdp.service").exists())

    def test_rdp_modes_publish_runtime_policy_without_precreating_tls(self) -> None:
        for mode, expected_bind in (("loopback", "127.0.0.1:3389"), ("lan", "0.0.0.0:3389")):
            with self.subTest(mode=mode):
                temporary, root, boot, payload, settings, storage = self.make_fixture()
                self.addCleanup(temporary.cleanup)
                settings = {**settings, "rdp_mode": mode}
                runner = FakeRunner(root, boot)
                original = installed._configure_boot
                installed._configure_boot = lambda *args, **kwargs: None
                old_umask = os.umask(0o077)
                try:
                    summary = installed.provision_target(
                        root,
                        payload,
                        settings,
                        storage,
                        lambda phase: None,
                        runner=runner,
                        machine="aarch64",
                    )
                finally:
                    installed._configure_boot = original
                    os.umask(old_umask)
                profile = root / "home/desk/.config/omarchy-pi-rdp/config.toml"
                self.assertIn(f'bind = "{expected_bind}"', profile.read_text(encoding="utf-8"))
                self.assertIn('username = "desk"', profile.read_text(encoding="utf-8"))
                self.assertEqual(stat.S_IMODE(profile.parent.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE((profile.parent / "password").stat().st_mode), 0o600)
                policy_path = root / "etc/omarchy-pi/rdp-profile.toml"
                self.assertEqual(stat.S_IMODE(policy_path.parent.stat().st_mode), 0o755)
                if os.geteuid() == 0:
                    self.assertEqual(policy_path.parent.stat().st_uid, 0)
                    self.assertEqual(policy_path.parent.stat().st_gid, 0)
                self.assert_readable_as_uid(policy_path, 1001)
                self.assertEqual(
                    policy_path.read_text(encoding="utf-8"),
                    f'username = "desk"\nbind = "{expected_bind}"\n',
                )
                self.assertFalse((root / "home/desk/.config/hypr-rdp").exists())
                self.assertEqual(summary["rdp_bind"], expected_bind)

                # Exercise the installed runtime guard against the policy and
                # profile emitted by the target provisioner.  The service sees
                # /home/desk after boot, so this fixture mirrors that namespace
                # while retaining the generated bind and username fields.
                runtime_home = Path(temporary.name) / "runtime-home"
                shutil.copytree(root / "home/desk/.config/omarchy-pi-rdp", runtime_home / ".config/omarchy-pi-rdp")
                runtime_config = runtime_home / ".config/omarchy-pi-rdp/config.toml"
                runtime_config.write_text(
                    runtime_config.read_text(encoding="utf-8").replace(
                        "/home/desk/.config/omarchy-pi-rdp/password",
                        str(runtime_home / ".config/omarchy-pi-rdp/password"),
                    ),
                    encoding="utf-8",
                )
                old_policy = rdp_runtime.PROFILE_POLICY
                old_owner = rdp_runtime.PACKAGE_OWNER_UID
                old_trusted = rdp_runtime.trusted_package_file
                rdp_runtime.PROFILE_POLICY = root / "etc/omarchy-pi/rdp-profile.toml"
                rdp_runtime.PACKAGE_OWNER_UID = os.getuid()
                rdp_runtime.trusted_package_file = lambda path, executable=False: rdp_runtime.path_info(
                    path,
                    owner_uid=os.getuid(),
                    executable=executable,
                    non_writable=True,
                )
                try:
                    with patch.object(
                        rdp_runtime.pwd,
                        "getpwuid",
                        return_value=SimpleNamespace(pw_name="desk"),
                    ):
                        self.assertEqual(
                            rdp_runtime.check_profile_policy(1001),
                            {"username": "desk", "bind": expected_bind},
                        )
                        rdp_runtime.check_config(runtime_home, 1001)
                finally:
                    rdp_runtime.PROFILE_POLICY = old_policy
                    rdp_runtime.PACKAGE_OWNER_UID = old_owner
                    rdp_runtime.trusted_package_file = old_trusted

                policy_path = root / "etc/omarchy-pi/rdp-profile.toml"
                account = installed._account_from_target(root, "desk")
                validated = installed.validate_settings(settings)
                checked_storage = installed._validate_storage(storage, validated)
                policy_path.parent.chmod(0o700)
                try:
                    with self.assertRaisesRegex(installed.TargetProvisionError, "RDP profile"):
                        installed._validate_result(root, account, validated, checked_storage, expected_bind)
                finally:
                    policy_path.parent.chmod(0o755)
                policy_path.write_text(
                    f'username = "desk"\nbind = "{"0.0.0.0:3389" if expected_bind == "127.0.0.1:3389" else "127.0.0.1:3389"}"\n',
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(installed.TargetProvisionError, "RDP profile"):
                    installed._validate_result(root, account, validated, checked_storage, expected_bind)
                policy_path.write_text(
                    f'username = "desk"\nbind = "{expected_bind}"\n',
                    encoding="utf-8",
                )
                validator_path = root / "usr/local/libexec/omarchy-pi/verify-hypr-rdp-runtime.py"
                validator_path.write_text("# stale bundle validator\n", encoding="ascii")
                with self.assertRaisesRegex(installed.TargetProvisionError, "RDP profile"):
                    installed._validate_result(root, account, validated, checked_storage, expected_bind)

        temporary, root, boot, payload, settings, storage = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        settings = {**settings, "rdp_mode": "disabled", "rdp_password": None}
        runner = FakeRunner(root, boot)
        original = installed._configure_boot
        installed._configure_boot = lambda *args, **kwargs: None
        try:
            summary = installed.provision_target(
                root,
                payload,
                settings,
                storage,
                lambda phase: None,
                runner=runner,
                machine="aarch64",
            )
        finally:
            installed._configure_boot = original
        self.assertEqual(summary["rdp_bind"], None)
        self.assertFalse((root / "etc/omarchy-pi/rdp-profile.toml").exists())
        self.assertFalse((root / "home/desk/.config/omarchy-pi-rdp").exists())
        self.assertFalse(
            (root / "home/desk/.config/systemd/user/graphical-session.target.wants/omarchy-pi-hypr-rdp.service").exists()
        )

    def test_rdp_resolution_is_optional_or_hypr_rdp_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            config_dir = home / ".config/omarchy-pi-rdp"
            config_dir.mkdir(parents=True, mode=0o700)
            password = config_dir / "password"
            password.write_bytes(b"rdp-secret")
            password.chmod(0o600)
            config = config_dir / "config.toml"
            base = (
                'bind = "127.0.0.1:3389"\n'
                'username = "desk"\n'
                f'password_file = "{password}"\n'
                "fps = 20\n"
                'egfx_codec = "avc420"\n'
                'audio_mode = "off"\n'
                'file_transfer_mode = "off"\n'
            )
            profile = {"username": "desk", "bind": "127.0.0.1:3389"}

            for resolution in (None, "1920x1080", "1919x1079", "65535x65535"):
                with self.subTest(resolution=resolution):
                    suffix = "" if resolution is None else f'resolution = "{resolution}"\n'
                    config.write_text(base + suffix, encoding="utf-8")
                    config.chmod(0o600)
                    rdp_runtime.check_config(home, os.getuid(), profile)

            for resolution in ("0x1080", "1x2", "65536x1080", "1920X1080", "1920x"):
                with self.subTest(resolution=resolution):
                    config.write_text(base + f'resolution = "{resolution}"\n', encoding="utf-8")
                    config.chmod(0o600)
                    with self.assertRaises(SystemExit):
                        rdp_runtime.check_config(home, os.getuid(), profile)

    def test_invalid_provenance_fails_before_target_commands(self) -> None:
        temporary, root, boot, payload, settings, storage = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        runner = FakeRunner(root, boot)
        with self.assertRaises(installed.TargetProvisionError) as context:
            installed.provision_target(
                root,
                payload,
                settings,
                storage,
                lambda phase: None,
                runner=runner,
                provenance={
                    "installer": {"source_revision": "not-a-revision", "runtime_sha256": "b" * 64},
                    "desktop_bundle_sha256": "c" * 64,
                },
            )
        self.assertEqual(runner.calls, [])
        self.assertNotIn("not-a-revision", str(context.exception))

    def test_encrypted_cmdline_has_uuid_arguments_without_key_material(self) -> None:
        temporary, root, boot, payload, settings, storage = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        settings = {**settings, "encryption": "key", "recovery_passphrase": "recovery-secret"}
        storage = {
            **storage,
            "luks_uuid": "33333333-3333-3333-3333-333333333333",
            "key_uuid": "44444444-4444-4444-4444-444444444444",
            # disk_install returns the public path in the disposable key
            # filesystem.  The provisioner must never read a host key path.
            "key_path": "/.cryptroot.key",
        }
        validated = installed.validate_settings(settings)
        checked = installed._validate_storage(storage, validated)
        installed._write_cmdline(root, checked, "key")
        cmdline = (boot / "cmdline.txt").read_text()
        self.assertIn("rd.luks.name=33333333-3333-3333-3333-333333333333=cryptroot", cmdline)
        self.assertIn("rd.luks.key=33333333-3333-3333-3333-333333333333=/.cryptroot.key:UUID=44444444-4444-4444-4444-444444444444", cmdline)
        self.assertNotIn("fixture-unlock-key", cmdline)
        self.assertNotIn("recovery-secret", cmdline)

    def test_host_fingerprint_matches_openssh_standard_base64(self) -> None:
        if shutil.which("ssh-keygen") is None:
            self.skipTest("ssh-keygen is unavailable")
        temporary = tempfile.TemporaryDirectory(prefix="omarchy-host-key-")
        self.addCleanup(temporary.cleanup)
        private = Path(temporary.name) / "fixture"
        generated = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(generated.returncode, 0, generated.stderr)
        root = Path(temporary.name) / "root"
        (root / "etc/ssh").mkdir(parents=True)
        shutil.copyfile(private.with_name("fixture.pub"), root / "etc/ssh/ssh_host_ed25519_key.pub")
        expected_output = subprocess.run(
            ["ssh-keygen", "-lf", str(private.with_name("fixture.pub")), "-E", "sha256"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        expected = next(item for item in expected_output if item.startswith("SHA256:"))
        self.assertEqual(installed._host_key_fingerprint(root), expected)


if __name__ == "__main__":
    unittest.main()
