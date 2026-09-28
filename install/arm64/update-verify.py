#!/usr/bin/python3
"""Verify protected Pi state after a package transaction."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path

from update_lib import UpdateCheckError, active_source_dir, compare_snapshots, load_json, snapshot, verify_boot_state, verify_custom_packages


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--baseline", required=True)
    result.add_argument("--root", default="/")
    result.add_argument("--home", default=None)
    result.add_argument("--source-dir", default=None)
    result.add_argument("--json", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    baseline = load_json(args.baseline)
    if not isinstance(baseline, dict):
        print(f"Could not read updater baseline: {args.baseline}", file=sys.stderr)
        return 1
    current_arch = os.environ.get("OMARCHY_PI_ARCH", platform.machine())
    if current_arch not in {"aarch64", "arm64"} and os.environ.get("OMARCHY_PI_TESTING") != "1":
        print(f"Refusing post-update verification on {current_arch}", file=sys.stderr)
        return 1
    if args.home is None and os.geteuid() == 0 and os.environ.get("OMARCHY_PI_TESTING") != "1":
        print("--home is required when verification runs as root", file=sys.stderr)
        return 1
    source_dir = args.source_dir or (str(active_source_dir(args.home)) if args.home and active_source_dir(args.home) else None)
    try:
        current = snapshot(root=args.root, home=args.home, source_dir=source_dir)
    except (UpdateCheckError, OSError) as exc:
        print(f"Protected-state verification failed: {exc}", file=sys.stderr)
        return 1
    failures = compare_snapshots(baseline, current)
    failures.extend(verify_boot_state(baseline, current, root=args.root))
    failures.extend(verify_custom_packages(baseline, root=args.root, source_dir=source_dir))
    result = {"ok": not failures, "failures": failures, "before": baseline, "after": current}
    if args.json:
        print(json.dumps(result, sort_keys=True))
    if failures:
        for failure in failures:
            print(f"Protected-state verification failed: {failure}", file=sys.stderr)
        return 1
    print("ARM boot, access, protected-state and custom-package checks passed", file=sys.stderr if args.json else sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
