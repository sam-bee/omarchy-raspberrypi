#!/usr/bin/python3
"""Apply only newly introduced, explicitly reviewed ARM migrations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def file_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def load_policy(path: Path) -> dict[str, dict[str, str]]:
    policy: dict[str, dict[str, str]] = {}
    if not path.is_file():
        return policy
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 4 or not fields[0].endswith(".sh") or fields[1] not in {"run", "skip"}:
            raise ValueError(f"{path}:{number}: expected migration.sh<TAB>run|skip<TAB>sha256<TAB>reason")
        if not re.fullmatch(r"[0-9a-f]{64}", fields[2]):
            raise ValueError(f"{path}:{number}: migration policy hash must be a lowercase SHA-256")
        if not fields[3].strip():
            raise ValueError(f"{path}:{number}: migration policy needs a review reason")
        if fields[0] in policy:
            raise ValueError(f"{path}:{number}: duplicate migration policy entry")
        policy[fields[0]] = {"action": fields[1], "sha256": fields[2], "reason": fields[3]}
    return policy


def migration_names(directory: Path) -> set[str]:
    return {path.name for path in directory.glob("*.sh") if path.is_file()}


def check_migrations(old_release: str | Path, new_release: str | Path, policy_path: str | Path) -> dict[str, object]:
    """Validate introduced and modified migrations against exact reviewed hashes."""

    old_dir = Path(old_release) / "migrations"
    new_dir = Path(new_release) / "migrations"
    policy = load_policy(Path(policy_path))
    old_names = migration_names(old_dir)
    new_names = migration_names(new_dir)
    introduced = sorted(new_names - old_names)
    changed = sorted(
        name for name in new_names & old_names
        if file_hash(old_dir / name) != file_hash(new_dir / name)
    )
    reviewed = sorted(set(introduced) | set(changed))
    failures: list[str] = []
    actions: dict[str, str] = {}
    for name in reviewed:
        entry = policy.get(name)
        actual_hash = file_hash(new_dir / name)
        if entry is None:
            failures.append(f"migration is new or changed without an exact ARM review: {name}")
            continue
        if entry["sha256"] != actual_hash:
            failures.append(f"migration policy hash does not match source: {name}")
            continue
        actions[name] = entry["action"]
    return {
        "introduced": introduced,
        "changed": changed,
        "reviewed": reviewed,
        "actions": actions,
        "failures": failures,
        "policy": policy,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--old-release", required=True)
    result.add_argument("--new-release", required=True)
    result.add_argument("--state-dir", default=None)
    result.add_argument("--policy", default=None)
    result.add_argument("--check", action="store_true")
    result.add_argument("--run", action="store_true")
    result.add_argument("--json", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    policy_path = Path(args.policy) if args.policy else Path(args.new_release) / "install/arm64/migrations.allowlist"
    try:
        checked = check_migrations(args.old_release, args.new_release, policy_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({key: value for key, value in checked.items() if key != "policy"}, sort_keys=True))
    if checked["failures"]:
        for failure in checked["failures"]:
            print(f"Refusing migration set: {failure}", file=sys.stderr)
        return 1
    if args.check or not args.run:
        if not args.json:
            print(f"ARM migration review passed ({len(checked['reviewed'])} reviewed new/changed migration(s))")
        return 0

    new_dir = Path(args.new_release) / "migrations"
    runnable = [name for name in checked["reviewed"] if checked["actions"].get(name) == "run"]
    state_dir = Path(args.state_dir or os.environ.get("OMARCHY_MIGRATION_STATE", str(Path.home() / ".local/state/omarchy/migrations")))
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in runnable:
        marker = state_dir / name
        if marker.is_file():
            if name in checked["changed"]:
                print(f"Migration {name} changed after its previous marker; manual review is required", file=sys.stderr)
                return 1
            continue
        script = new_dir / name
        message = f"Running reviewed ARM migration {name}"
        print(message, file=sys.stderr if args.json else sys.stdout)
        environment = os.environ.copy()
        environment["OMARCHY_PATH"] = str(Path(args.new_release))
        environment["PATH"] = str(Path(args.new_release) / "bin") + ":" + environment.get("PATH", "/usr/bin:/bin")
        try:
            subprocess.run(["/bin/bash", "-euo", "pipefail", str(script)], check=True, env=environment)
        except subprocess.CalledProcessError as exc:
            print(f"Migration {name} failed with status {exc.returncode}; rerun after review", file=sys.stderr)
            return exc.returncode or 1
        marker.touch()
    skipped = [name for name in checked["reviewed"] if checked["actions"].get(name) == "skip"]
    if skipped:
        print("Skipped reviewed Pi-inapplicable migrations: " + ", ".join(skipped), file=sys.stderr if args.json else sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
