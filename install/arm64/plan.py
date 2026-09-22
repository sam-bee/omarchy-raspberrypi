"""Read-only ARM64 plan. No subprocesses, package transactions or system writes."""

import argparse
import json
import platform
import re
import sys
from collections import Counter
from pathlib import Path

BASELINE = "947e2fc002d6831c7888b29b5761d59d29e69727"
ROOT = Path(__file__).resolve().parents[2]
PACKAGE_NAME = re.compile(r"[a-z0-9@_+][a-z0-9@._+\-]*\Z")
PRESERVE = [
    "Raspberry Pi EEPROM boot order and firmware boot chain",
    "linux-rpi, /boot/config.txt, /boot/cmdline.txt and existing initramfs",
    "mkinitcpio hooks: retain sd-encrypt and do not add kms",
    "LUKS2 root, keyslots, USB auto-unlock key and rescue USB partitions",
    "Arch Linux ARM pacman repositories, mirrors, keyring and package hooks",
    "systemd-networkd, wpa_supplicant@wld0 and existing DHCP/Wi-Fi configuration",
    "sshd, current SSH access, firewall rules and enabled services",
    "Existing user files; no wholesale /etc/skel or /etc deployment",
]
BLOCKERS = [
    "No apply implementation exists in this version; a successful plan is not installation approval.",
    "Recheck package names, dependency closure and compatible versions against current target repositories before deployment.",
    "Verify Hyprland Lua configuration support, Quickshell dependencies and Omarchy session packaging together.",
    "Resolve deferred desktop helpers and portal picker before claiming a complete upstream desktop.",
    "Review package dependencies, conflicts and transaction hooks for effects on the protected substrate.",
    "Design backed-up, allow-listed user/session asset deployment and rollback for the existing sierra account.",
    "Live desktop, unattended boot/unlock and headless/RDP integration remain untested.",
    "Normal Omarchy install/update/reset workflows remain unsupported on ARM; guards are not a sandbox.",
]


def detect_target(target, machine=None, model=None):
    if target != "auto":
        return {"architecture": "aarch64", "profile": target, "source": "explicit"}
    architecture = (machine or platform.machine()).lower()
    if architecture == "arm64":
        architecture = "aarch64"
    if architecture != "aarch64":
        raise ValueError(f"host architecture {architecture!r} is not ARM64; use --target rpi5 for an offline preview")
    if model is None:
        model = ""
        for filename in ("/sys/firmware/devicetree/base/model", "/proc/device-tree/model"):
            try:
                model = Path(filename).read_bytes().rstrip(b"\0").decode("utf-8", errors="replace")
                break
            except FileNotFoundError:
                continue
    profile = "rpi5" if re.match(r"^Raspberry Pi 5(?:\s|$)", model) else "arm64"
    return {"architecture": architecture, "profile": profile, "source": "host", "model": model}


def read_policy(path):
    rows = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 4:
            raise ValueError(f"{path.name}:{number}: expected four tab-separated fields")
        package, action, replacement, reason = fields
        if not PACKAGE_NAME.fullmatch(package) or package in rows:
            raise ValueError(f"{path.name}:{number}: invalid or duplicate package {package!r}")
        if action not in {"candidate", "replace", "defer", "exclude"} or not reason.strip():
            raise ValueError(f"{path.name}:{number}: invalid action or missing reason")
        if action == "replace":
            if not PACKAGE_NAME.fullmatch(replacement) or replacement == package:
                raise ValueError(f"{path.name}:{number}: replacement must name a different package")
        elif replacement != "-":
            raise ValueError(f"{path.name}:{number}: replacement requires the replace action")
        rows[package] = dict(zip(("package", "action", "replacement", "reason"), fields))
    return rows


def build_plan(target):
    base = [line.split("#", 1)[0].strip() for line in (ROOT / "install/omarchy-base.packages").read_text().splitlines()]
    base = [name for name in base if name]
    if len(base) != len(set(base)) or any(not PACKAGE_NAME.fullmatch(name) for name in base):
        raise ValueError("upstream base package list has duplicate or invalid entries")
    policy = read_policy(ROOT / "install/arm64/packages.tsv")
    if set(base) != set(policy):
        missing = sorted(set(base) - set(policy))
        stale = sorted(set(policy) - set(base))
        raise ValueError(f"package policy drift: unclassified={missing}, removed upstream={stale}; review policy before planning")
    extras = read_policy(ROOT / "install/arm64/packages-extra.tsv")
    if set(extras) & set(policy):
        raise ValueError("extra package policy duplicates upstream package policy")
    packages = [policy[name] for name in base]
    packages.extend(extras.values())
    # Broadcom Vulkan is a Pi profile candidate, not generic ARM GPU policy.
    if target["profile"] != "rpi5":
        packages = [row for row in packages if row["package"] != "vulkan-broadcom"]
    return {
        "schema_version": 1,
        "mode": "plan-only",
        "baseline": BASELINE,
        "target": target,
        "ready_to_apply": False,
        "allowed_system_changes": [],
        "preserve": PRESERVE,
        "packages": packages,
        "blockers": BLOCKERS,
    }


def render_text(plan):
    print("Omarchy ARM64 compatibility plan — PLAN ONLY")
    print(f"Official baseline: {plan['baseline']}")
    target = plan["target"]
    print(f"Target: {target['architecture']} / {target['profile']} ({target['source']})")
    if target["source"] == "explicit":
        print("Target is an assumption for offline review; no Pi connection or inventory was performed.")
    print("Allowed system changes: none. Ready to apply: no.")
    print("\nProtected substrate (preservation policy, not a live verification):")
    for item in plan["preserve"]:
        print(f"  PRESERVE {item}")
    print("\nPackage policy (availability and dependency effects unverified):")
    for row in plan["packages"]:
        replacement = f" -> {row['replacement']}" if row["action"] == "replace" else ""
        print(f"  {row['action'].upper():9} {row['package']}{replacement}: {row['reason']}")
    counts = Counter(row["action"] for row in plan["packages"])
    print("\nCounts: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    print("\nBefore a separate deployment milestone:")
    for item in plan["blockers"]:
        print(f"  BLOCKED {item}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("auto", "rpi5", "arm64"), default="auto")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    try:
        plan = build_plan(detect_target(args.target))
    except (OSError, ValueError) as error:
        parser.exit(1, f"Cannot produce plan: {error}\n")
    if args.format == "json":
        print(json.dumps(plan, indent=2))
    else:
        render_text(plan)


if __name__ == "__main__":
    main()
