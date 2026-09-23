#!/bin/bash

set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

python3 - "$ROOT" <<'PY' || fail "RDP profile setup preserves private credentials and existing files"
import importlib.util
import os
import stat
import sys
import tempfile
import tomllib
from pathlib import Path

path = Path(sys.argv[1]) / "install/arm64/setup-rdp-credentials.py"
spec = importlib.util.spec_from_file_location("rdp_profile", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
verifier_path = Path(sys.argv[1]) / "install/arm64/session/systemd/verify-hypr-rdp-runtime.py"
verifier_spec = importlib.util.spec_from_file_location("rdp_verifier", verifier_path)
verifier = importlib.util.module_from_spec(verifier_spec)
verifier_spec.loader.exec_module(verifier)

with tempfile.TemporaryDirectory(prefix="pi-rdp-profile-") as scratch:
    home = Path(scratch) / 'home with "quotes"'
    home.mkdir(mode=0o700)
    uid = os.getuid()
    config, password = module.create_profile(home, uid, "secret with spaces")
    assert verifier.check_config(home, uid) == (config.parent, password)
    parsed = tomllib.loads(config.read_text())
    assert parsed == {
        "bind": "127.0.0.1:3389",
        "username": "omarchy-pi",
        "password_file": str(password),
        "resolution": "1280x720",
        "fps": 20,
        "egfx_codec": "avc420",
        "audio_mode": "off",
        "file_transfer_mode": "off",
    }
    assert password.read_bytes() == b"secret with spaces"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE(password.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700
    assert not list(config.parent.glob(".omarchy-pi.*"))

    try:
        module.create_profile(home, uid, "replacement")
        raise AssertionError("existing profile was overwritten")
    except FileExistsError:
        pass
    assert password.read_bytes() == b"secret with spaces"

    alternate = Path(scratch) / "alternate"
    alternate.mkdir(mode=0o700)
    (alternate / ".config").symlink_to(home / ".config", target_is_directory=True)
    try:
        module.create_profile(alternate, uid, "replacement")
        raise AssertionError("symlinked configuration parent was accepted")
    except ValueError:
        pass

    empty = Path(scratch) / "empty"
    empty.mkdir(mode=0o700)
    try:
        module.create_profile(empty, uid, "line\nbreak")
        raise AssertionError("line break in password was accepted")
    except ValueError:
        pass
    assert not (empty / ".config").exists()

    failed = Path(scratch) / "failed"
    failed.mkdir(mode=0o700)
    original = module.create_file
    def fail_second_write(target, payload):
        if target.name == "config.toml":
            raise OSError("simulated config publication failure")
        original(target, payload)
    module.create_file = fail_second_write
    try:
        module.create_profile(failed, uid, "temporary secret")
        raise AssertionError("simulated publication failure did not propagate")
    except OSError:
        pass
    assert not (failed / ".config/omarchy-pi-rdp/password").exists()
    assert not (failed / ".config/omarchy-pi-rdp/config.toml").exists()
PY
pass "RDP profile setup creates private loopback credentials and preserves existing files"
