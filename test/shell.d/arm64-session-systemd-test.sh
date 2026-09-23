#!/bin/bash

set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

python3 - "$ROOT/install/arm64/session/systemd/verify-hypr-rdp-runtime.py" <<'PY'
import contextlib
import hashlib
import importlib.util
import io
import os
import pwd
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


verifier_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("verify_hypr_rdp_runtime", verifier_path)
assert spec is not None and spec.loader is not None
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)

uid = os.getuid()
with tempfile.TemporaryDirectory(prefix="pi-session-") as scratch:
    root = Path(scratch)
    home = root / "user home with spaces"
    runtime_root = root / "runtime root"
    runtime = runtime_root / str(uid)
    package_root = root / "package"
    binary = package_root / "bin/hypr-rdp"
    manifest = package_root / "share/omarchy-pi/hypr-rdp.sha256"
    config_dir = home / ".config/omarchy-pi-rdp"
    wayland_name = "wayland-rdp-test"

    for directory, mode in (
        (home, 0o700),
        (home / ".config", 0o700),
        (config_dir, 0o700),
        (runtime, 0o700),
        (binary.parent, 0o700),
        (manifest.parent, 0o700),
    ):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(mode)

    binary.write_bytes(b"fixture pinned executable\n")
    binary.chmod(0o755)
    manifest.write_text(hashlib.sha256(binary.read_bytes()).hexdigest() + "\n")
    manifest.chmod(0o644)

    password_path = config_dir / "password"
    password_path.write_text("fixture-password\n")
    password_path.chmod(0o600)

    def write_config(*, bind="127.0.0.1:3389", extra=""):
        password_file = str(password_path)
        config = (
            f'bind = "{bind}"\n'
            'username = "omarchy-pi"\n'
            'resolution = "1280x720"\n'
            'fps = 20\n'
            'egfx_codec = "avc420"\n'
            'audio_mode = "off"\n'
            'file_transfer_mode = "off"\n'
            f'password_file = "{password_file}"\n'
            f"{extra}"
        )
        config_path = config_dir / "config.toml"
        config_path.write_text(config)
        config_path.chmod(0o600)

    write_config()
    socket_path = runtime / wayland_name
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(socket_path))
    sock.close()

    fake_passwd = SimpleNamespace(pw_dir=str(home))
    verifier.BINARY = binary
    verifier.SHA256_FILE = manifest
    verifier.PACKAGE_OWNER_UID = uid
    verifier.RUNTIME_ROOT = runtime_root
    verifier.trusted_package_file = lambda path, executable=False: verifier.path_info(
        path,
        owner_uid=uid,
        executable=executable,
        non_writable=True,
    )

    def run(
        *,
        uid_arg=uid,
        home_arg=str(home),
        display=wayland_name,
        home_env=str(home),
    ):
        env = {
            "HOME": home_env,
            "XDG_RUNTIME_DIR": str(runtime),
            "WAYLAND_DISPLAY": display,
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(verifier.pwd, "getpwuid", return_value=fake_passwd), patch.dict(
            os.environ, env, clear=True
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                verifier.main(
                    ["--uid", str(uid_arg), "--home", home_arg, "--runtime", str(runtime)]
                )
            except SystemExit as exc:
                return exc.code, stdout.getvalue(), stderr.getvalue()
        return 0, stdout.getvalue(), stderr.getvalue()

    code, stdout, stderr = run()
    assert code == 0, stderr
    assert "bind=127.0.0.1:3389" in stdout
    assert f"sha256={hashlib.sha256(binary.read_bytes()).hexdigest()}" in stdout

    code, _, stderr = run(uid_arg=uid + 1)
    assert code == 1 and "UID does not match" in stderr

    code, _, stderr = run(home_arg=str(home) + "/wrong")
    assert code == 1 and "home does not match" in stderr
    code, _, stderr = run(home_env=str(home) + "/wrong")
    assert code == 1 and "HOME does not match" in stderr

    code, _, stderr = run(display="../wayland-rdp-test")
    assert code == 1 and "WAYLAND_DISPLAY" in stderr

    write_config(bind="0.0.0.0:3389")
    code, _, stderr = run()
    assert code == 1 and "config field bind" in stderr
    write_config(extra='password = "must-not-be-in-config"\n')
    code, _, stderr = run()
    assert code == 1 and "plaintext password" in stderr

    write_config()
    tls_dir = home / ".config/hypr-rdp"
    tls_dir.mkdir(mode=0o700)
    (tls_dir / "cert.pem").write_text("partial certificate pair\n")
    (tls_dir / "cert.pem").chmod(0o644)
    code, _, stderr = run()
    assert code == 1 and "pair is incomplete" in stderr
    shutil.rmtree(tls_dir)

    password_path.unlink()
    password_path.symlink_to(root / "outside-password")
    (root / "outside-password").write_text("fixture-password\n")
    (root / "outside-password").chmod(0o600)
    code, _, stderr = run()
    assert code == 1 and "non-symlink" in stderr

    password_path.unlink()
    password_path.write_text("fixture-password\n")
    password_path.chmod(0o600)
    binary.write_bytes(b"changed after manifest generation\n")
    code, _, stderr = run()
    assert code == 1 and "SHA-256 differs" in stderr

print("account-independent verifier fixtures passed")
PY

pass "RDP verifier resolves the active UID and a home path containing spaces"
pass "RDP verifier rejects mismatched identity, unsafe display/profile paths, and non-loopback binds"
pass "RDP verifier rejects plaintext and symlinked passwords plus a changed package binary"

systemd_dir="$ROOT/install/arm64/session/systemd"
if rg -n '/home/sierra|/run/user/1000|UID *= *1000|hypr-rdp-v0\.1\.6-build' "$systemd_dir"; then
  fail "installed systemd session sources contain machine-specific runtime paths"
fi
if ! rg -q '^User=%i$' "$systemd_dir/omarchy-pi-uwsm-session@.service"; then
  fail "UWSM template does not select its account from the instance name"
fi
if ! rg -q -- '--uid %U --home \$\{HOME\} --runtime %t' "$systemd_dir/omarchy-pi-hypr-rdp.service"; then
  fail "RDP unit does not pass user-manager identity specifiers to the verifier"
fi
pass "systemd templates contain no Sierra, UID 1000, or dated build path"
