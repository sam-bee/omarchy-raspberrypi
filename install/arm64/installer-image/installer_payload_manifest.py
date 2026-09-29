"""Static inventory shared by installer payload staging and verification.

Keep this module data-only.  The stage and verify paths still perform their
own semantic checks; this file only prevents their declared payload contract
from drifting apart.
"""

BUILDER_MARKER = "usr/lib/omarchy-pi/installer-image.marker"
BUILDER_MARKER_CONTENT = b"omarchy-pi-installer-image-v1\n"
EXAMPLE_SETTINGS = "installer-settings.example.toml"
SETTINGS_FILE = "installer-settings.toml"
NETWORKD_PRESET = "etc/systemd/system-preset/00-omarchy-installer-networkd.preset"
NETWORKD_PRESET_CONTENT = b"disable systemd-networkd*\n"

LIBEXEC_FILES = {
    "settings.py": 0o644,
    "disk_install.py": 0o644,
    "installer_job.py": 0o644,
    "recovery.py": 0o644,
    "installed_target.py": 0o644,
    "desktop_payload.py": 0o644,
    "configure-installer-boot.py": 0o644,
    "assemble-image.py": 0o644,
    "installer-control": 0o755,
    "provision-access.py": 0o755,
    "provision-network.py": 0o755,
    "provision-rdp.py": 0o755,
    "verify-installer-rdp-runtime.py": 0o755,
    "launch-installer-session.py": 0o755,
    "start-installer-session.sh": 0o755,
    "start-installer-desktop.sh": 0o755,
}
SHARE_FILES = {"installer-hyprland.conf": 0o644}
SYSTEM_UNITS = {
    "omarchy-pi-install.service": 0o644,
    "omarchy-pi-provision-access.service": 0o644,
    "omarchy-pi-provision-network.service": 0o644,
    "omarchy-pi-provision-rdp.service": 0o644,
    "omarchy-installer-launch.service": 0o644,
    "omarchy-installer-session@.service": 0o644,
}
USER_UNITS = {"omarchy-installer-rdp.service": 0o644}
BOOT_ENABLED_UNITS = (
    "omarchy-pi-provision-access.service",
    "omarchy-pi-provision-network.service",
    "omarchy-pi-provision-rdp.service",
    "omarchy-installer-launch.service",
)

PUBLIC_PAYLOAD_DIRECTORIES = (
    "usr",
    "usr/local",
    "usr/local/bin",
    "usr/local/libexec",
    "usr/local/libexec/omarchy-pi",
    "usr/local/share",
    "usr/local/share/omarchy-pi",
    "usr/lib",
    "usr/lib/omarchy-pi",
    "usr/bin",
    "usr/share",
    "usr/share/omarchy-pi",
    "etc",
    "etc/systemd",
    "etc/systemd/system",
    "etc/systemd/system-preset",
    "etc/systemd/user",
    "etc/systemd/system/multi-user.target.wants",
    "etc/systemd/system/network-pre.target.requires",
    "etc/systemd/system/sshd.service.requires",
    "etc/systemd/system/NetworkManager.service.requires",
    "etc/systemd/user/graphical-session.target.wants",
)

EXECUTABLE_PAYLOAD_FILES = tuple(
    "usr/local/libexec/omarchy-pi/" + name
    for name, mode in LIBEXEC_FILES.items()
    if mode == 0o755
) + (
    # These are installed from adjacent sources rather than LIBEXEC_FILES.
    "usr/local/bin/omarchy-pi-install",
    "usr/local/bin/omarchy-pi-recover",
    "usr/local/libexec/omarchy-pi/verify-hypr-rdp-runtime.py",
    "usr/bin/hypr-rdp",
)

PROVENANCE_MODULE_FILES = tuple(
    "usr/local/libexec/omarchy-pi/" + name
    for name, mode in LIBEXEC_FILES.items()
    if mode == 0o644
)

EXPECTED_LINKS = {
    "etc/systemd/system/multi-user.target.wants/NetworkManager.service": "/usr/lib/systemd/system/NetworkManager.service",
    "etc/systemd/system/multi-user.target.wants/sshd.service": "/usr/lib/systemd/system/sshd.service",
    "etc/systemd/system/multi-user.target.wants/omarchy-pi-provision-access.service": "/etc/systemd/system/omarchy-pi-provision-access.service",
    "etc/systemd/system/multi-user.target.wants/omarchy-pi-provision-network.service": "/etc/systemd/system/omarchy-pi-provision-network.service",
    "etc/systemd/system/multi-user.target.wants/omarchy-pi-provision-rdp.service": "/etc/systemd/system/omarchy-pi-provision-rdp.service",
    "etc/systemd/system/multi-user.target.wants/omarchy-installer-launch.service": "/etc/systemd/system/omarchy-installer-launch.service",
    "etc/systemd/system/network-pre.target.requires/omarchy-pi-provision-access.service": "/etc/systemd/system/omarchy-pi-provision-access.service",
    "etc/systemd/system/network-pre.target.requires/omarchy-pi-provision-network.service": "/etc/systemd/system/omarchy-pi-provision-network.service",
    "etc/systemd/system/sshd.service.requires/omarchy-pi-provision-access.service": "/etc/systemd/system/omarchy-pi-provision-access.service",
    "etc/systemd/system/sshd.service.requires/omarchy-pi-provision-network.service": "/etc/systemd/system/omarchy-pi-provision-network.service",
    "etc/systemd/system/NetworkManager.service.requires/omarchy-pi-provision-network.service": "/etc/systemd/system/omarchy-pi-provision-network.service",
    "etc/systemd/user/graphical-session.target.wants/omarchy-installer-rdp.service": "../omarchy-installer-rdp.service",
}
