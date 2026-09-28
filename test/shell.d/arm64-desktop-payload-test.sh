#!/bin/bash

set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

require_command python3

python3 - "$ROOT" <<'PY'
import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile

root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    "build_desktop_payload", root / "install/arm64/build-desktop-payload.py"
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def expect_error(function):
    try:
        function()
    except module.DesktopPayloadError:
        return
    raise AssertionError("expected DesktopPayloadError")


with tempfile.TemporaryDirectory(prefix="omarchy-payload-test-") as temporary:
    target = Path(temporary) / "rootfs"
    etc = target / "etc"
    (etc / "ssh").mkdir(parents=True)
    (target / "home").mkdir()
    (target / "root").mkdir()
    (etc / "passwd").write_text(
        "root:x:0:0:root:/root:/bin/bash\n"
        "alarm:x:1000:1000:alarm:/home/alarm:/bin/bash\n",
        encoding="utf-8",
    )
    (etc / "shadow").write_text(
        "root:$6$stock-root-secret:19000:0:99999:7:::\n"
        "alarm:!:19000:0:99999:7:::\n",
        encoding="utf-8",
    )
    (etc / "gshadow").write_text(
        "root:!::\n"
        "alarm:!::\n",
        encoding="utf-8",
    )
    (etc / "machine-id").write_text("", encoding="utf-8")
    (etc / "hostname").write_text("", encoding="utf-8")

    module._lock_stock_root_password(target)
    for backup in ("passwd-", "shadow-", "group-", "gshadow-"):
        (etc / backup).write_text("stock account backup\n", encoding="utf-8")
    module._remove_account_backups(target)
    assert not any((etc / backup).exists() for backup in ("passwd-", "shadow-", "group-", "gshadow-"))
    root_shadow = next(
        line for line in (etc / "shadow").read_text(encoding="utf-8").splitlines()
        if line.startswith("root:")
    )
    assert root_shadow.split(":")[1] == "!"

    for filename in ("passwd", "shadow", "gshadow"):
        path = etc / filename
        path.write_text(
            "\n".join(
                line for line in path.read_text(encoding="utf-8").splitlines()
                if not line.startswith("alarm:")
            )
            + "\n",
            encoding="utf-8",
        )
    (target / "root/.ssh").mkdir()
    (target / "root/.cache").mkdir()
    module._validate_generic_root(target)
    (target / "root/.cache/private-file").write_text("must not ship")
    expect_error(lambda: module._validate_generic_root(target))
    (target / "root/.cache/private-file").unlink()
    module._validate_generic_accounts(target)

    (etc / "passwd").write_text(
        (etc / "passwd").read_text(encoding="utf-8")
        + "desktop:x:1000:1000:desktop:/home/desktop:/bin/bash\n",
        encoding="utf-8",
    )
    expect_error(lambda: module._validate_generic_accounts(target))

    (etc / "passwd").write_text(
        "root:x:0:0:root:/root:/bin/bash\n",
        encoding="utf-8",
    )
    (etc / "machine-id").write_text("generated-id\n", encoding="utf-8")
    expect_error(lambda: module._validate_generic_root(target))

    fake_pacman = Path(temporary) / "pacman"
    fake_pacman.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'hypr-rdp 0.1.6-1'\n",
        encoding="utf-8",
    )
    fake_pacman.chmod(fake_pacman.stat().st_mode | stat.S_IXUSR)
    archive = Path(temporary) / "hypr-rdp-0.1.6-1-aarch64.pkg.tar.zst"
    archive.write_bytes(b"fixture archive")
    record = {"package": "hypr-rdp", "version": "0.1.6-1"}
    module._validate_custom_archive_metadata(str(fake_pacman), archive, record)
    expect_error(
        lambda: module._validate_custom_archive_metadata(
            str(fake_pacman), archive, {"package": "ttfx", "version": "0.3.2-1"}
        )
    )
    module._validate_installed_custom_packages(
        [{"name": "hypr-rdp", "version": "0.1.6-1"}],
        [archive],
        {archive.name: record},
    )
    expect_error(
        lambda: module._validate_installed_custom_packages(
            [{"name": "hypr-rdp", "version": "0.1.5-1"}],
            [archive],
            {archive.name: record},
        )
    )

print("desktop payload identity and custom metadata checks passed")
PY

pass "desktop payload identity and custom metadata guards"
