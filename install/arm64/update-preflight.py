#!/usr/bin/python3
"""Read-only ARM updater preflight and protected-state baseline capture."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sys
from pathlib import Path

from update_lib import UpdateCheckError, active_source_dir, atomic_json, command_output, compare_recipe_hashes, snapshot, state_path, verify_boot_state


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--state-dir", default=None)
    result.add_argument("--root", default="/", help="target root, for fixture tests")
    result.add_argument("--home", default=None)
    result.add_argument("--source-dir", default=None)
    result.add_argument("--old-release", default=None)
    result.add_argument("--new-release", default=None)
    result.add_argument("--baseline", default=None)
    result.add_argument("--json", action="store_true")
    return result


def fail(message: str, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"ok": False, "error": message}, sort_keys=True))
    else:
        print(f"ARM update preflight failed: {message}", file=sys.stderr)
    return 1


def pacman_signature_failures(config: str, *, root: str) -> list[str]:
    """Reject unsigned package policy, including effective per-repository settings."""

    failures: list[str] = []
    for line in config.splitlines():
        match = re.match(r"^\s*SigLevel\s*=\s*(.*?)\s*(?:#.*)?$", line, re.IGNORECASE)
        if not match:
            continue
        tokens = {token.lower() for token in match.group(1).split()}
        if {"never", "optional", "packagenever", "packageoptional", "trustall", "packagetrustall"} & tokens:
            failures.append("pacman.conf contains an unsigned or untrusted SigLevel")
    if root != "/" or os.environ.get("OMARCHY_PI_TESTING") == "1":
        return failures
    pacman_conf = shutil.which("pacman-conf")
    if pacman_conf is None:
        return failures + ["pacman-conf is unavailable; effective repository signature policy cannot be verified"]
    status, output = command_output([pacman_conf, "--repo-list"])
    if status != 0:
        return failures + ["pacman-conf could not enumerate repositories"]
    repositories = [line.strip() for line in output.splitlines() if line.strip()]
    if not repositories:
        return failures + ["pacman-conf returned no repositories"]
    for repository in repositories:
        status, output = command_output([pacman_conf, "--repo", repository, "SigLevel"])
        if status != 0:
            failures.append(f"pacman-conf could not read effective SigLevel for {repository}")
            continue
        tokens = {token.lower() for token in re.split(r"[\s=]+", output) if token}
        if {"never", "optional", "packagenever", "packageoptional", "trustall", "packagetrustall"} & tokens:
            failures.append(f"repository {repository} does not require trusted package signatures")
        if not {"required", "packagerequired"} & tokens:
            failures.append(f"repository {repository} lacks PackageRequired signature policy")
    return failures


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    architecture = os.environ.get("OMARCHY_PI_ARCH", platform.machine())
    if architecture not in {"aarch64", "arm64"} and os.environ.get("OMARCHY_PI_TESTING") != "1":
        return fail(f"unsupported architecture {architecture}; ARM64 is required", as_json=args.json)
    if args.home is None and os.geteuid() == 0 and os.environ.get("OMARCHY_PI_TESTING") != "1":
        return fail("--home is required when preflight runs as root", as_json=args.json)
    pacman_conf = Path("/etc/pacman.conf") if args.root == "/" else Path(args.root) / "etc/pacman.conf"
    if not pacman_conf.is_file():
        return fail("/etc/pacman.conf is missing; this does not look like an Arch Linux ARM install", as_json=args.json)
    config = pacman_conf.read_text(errors="replace")
    signature_failures = pacman_signature_failures(config, root=args.root)
    if signature_failures:
        return fail("; ".join(signature_failures), as_json=args.json)
    if args.root == "/" and os.environ.get("OMARCHY_PI_TESTING") != "1":
        pacman = os.environ.get("OMARCHY_PI_PACMAN", "pacman")
        if not any(Path(directory, pacman).is_file() for directory in ("/usr/bin", "/bin")) and not Path(pacman).is_absolute():
            return fail(f"pacman is unavailable: {pacman}", as_json=args.json)

    recipe_failures = []
    if bool(args.old_release) != bool(args.new_release):
        return fail("--old-release and --new-release must be supplied together", as_json=args.json)
    if args.old_release:
        recipe_failures = compare_recipe_hashes(args.old_release, args.new_release)
        if recipe_failures:
            return fail("; ".join(recipe_failures), as_json=args.json)

    source_dir = args.source_dir or (str(active_source_dir(args.home)) if args.home and active_source_dir(args.home) else None)
    try:
        result = snapshot(root=args.root, home=args.home, source_dir=source_dir)
    except (UpdateCheckError, OSError) as exc:
        return fail(str(exc), as_json=args.json)
    baseline_failures = verify_boot_state(result, result, root=args.root)
    if baseline_failures:
        return fail("; ".join(baseline_failures), as_json=args.json)
    baseline = args.baseline
    if baseline is None and args.state_dir:
        baseline = str(state_path(args.state_dir, "baseline.json"))
    if baseline:
        atomic_json(baseline, result)
    output = {"ok": True, "architecture": architecture, "baseline": baseline, "snapshot": result, "recipe_failures": recipe_failures}
    if args.json:
        print(json.dumps(output, sort_keys=True))
    else:
        print(f"ARM update preflight passed ({architecture})")
        if baseline:
            print(f"Protected-state baseline: {baseline}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
