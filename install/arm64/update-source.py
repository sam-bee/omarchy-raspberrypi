#!/usr/bin/python3
"""Prepare and activate user-owned Raspberry Pi source releases.

The Pi session stager deliberately accepts only a clean Git checkout.  This
helper therefore keeps a private bare cache and detached clean checkouts,
then invokes the stager from one of those checkouts.  The stager remains the only
writer of ``releases/``, ``current`` and ``previous``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


SOURCE_URL = "https://github.com/sam-bee/omarchy-raspberrypi.git"
SOURCE_BRANCH = "quattro-rpi5"
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_TIMEOUT = 45


class UpdateError(Exception):
    """A user-actionable source update failure."""


def command_text(command: list[str], *, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run a noninteractive command and return trimmed stdout."""

    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise UpdateError(f"required command is unavailable: {command[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise UpdateError(f"command timed out: {command[0]}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise UpdateError(f"command failed ({result.returncode}): {' '.join(command[:3])}: {detail}")
    return result.stdout.strip()


def git_text(repository: Path | None, arguments: list[str], *, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run git with a repository path, using --git-dir for bare repositories."""

    if repository is None:
        command = ["git", *arguments]
    elif (repository / "HEAD").is_file() and not (repository / ".git").exists():
        command = ["git", "--git-dir", str(repository), *arguments]
    else:
        command = ["git", "-C", str(repository), *arguments]
    return command_text(command, timeout=timeout)


def require_user_home() -> Path:
    if os.geteuid() == 0:
        raise UpdateError("run the Pi source updater as the target user without sudo")
    home_text = os.environ.get("HOME", "")
    if not home_text or not os.path.isabs(home_text):
        raise UpdateError("HOME must be an absolute path")
    home = Path(home_text).resolve()
    if not home.is_dir():
        raise UpdateError(f"HOME is not a directory: {home}")
    return home


def ensure_private_directory(path: Path) -> None:
    """Create a user-owned 0700 directory and refuse links/other owners."""

    if path.is_symlink():
        raise UpdateError(f"refusing a symlinked private directory: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise UpdateError(f"private path is not a directory: {path}")
    if path.stat().st_uid != os.geteuid():
        raise UpdateError(f"private directory is not owned by the target user: {path}")
    path.chmod(0o700)


def paths(home: Path) -> dict[str, Path]:
    data = home / ".local/share/omarchy-pi"
    cache = home / ".cache/omarchy-pi"
    return {
        "data": data,
        "releases": data / "releases",
        "current": data / "current",
        "previous": data / "previous",
        "cache": cache,
        "bare": cache / "source.git",
        "checkouts": cache / "checkouts",
    }


def validate_revision(value: str) -> str:
    revision = value.strip().lower()
    if not REVISION_RE.fullmatch(revision):
        raise UpdateError(f"source revision is not a full commit SHA: {value}")
    return revision


def release_revision(pointer: Path, data: Path) -> str | None:
    """Read a stager pointer without trusting arbitrary symlink targets."""

    if not pointer.exists() and not pointer.is_symlink():
        return None
    if not pointer.is_symlink():
        raise UpdateError(f"refusing a non-symlink release pointer: {pointer}")
    target = os.readlink(pointer)
    match = re.fullmatch(r"releases/([0-9a-f]{40})", target)
    if not match:
        raise UpdateError(f"refusing an invalid release pointer: {pointer}")
    release = data / target
    marker = release / ".omarchy-pi-source-commit"
    if not release.is_dir() or release.is_symlink() or not marker.is_file() or marker.is_symlink():
        raise UpdateError(f"refusing an incomplete release pointer: {pointer}")
    if marker.read_text(encoding="utf-8").strip() != match.group(1):
        raise UpdateError(f"release marker does not match its pointer: {release}")
    return match.group(1)


def current_state(home: Path) -> tuple[str | None, str | None]:
    location = paths(home)
    data = location["data"]
    if data.is_symlink() or location["releases"].is_symlink():
        raise UpdateError("refusing a symlinked Pi source data directory")
    current = release_revision(location["current"], data)
    previous = release_revision(location["previous"], data)
    if current and previous and current == previous:
        raise UpdateError("current and previous release pointers are identical")
    return current, previous


def remote_revision() -> str:
    output = command_text(
        ["git", "ls-remote", "--refs", SOURCE_URL, f"refs/heads/{SOURCE_BRANCH}"],
        timeout=DEFAULT_TIMEOUT,
    )
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == f"refs/heads/{SOURCE_BRANCH}" and REVISION_RE.fullmatch(fields[0]):
            return fields[0]
    raise UpdateError(f"source branch was not found: {SOURCE_BRANCH}")


def output(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    status = payload.get("status")
    if status == "up-to-date":
        print(f"up-to-date {payload['remote_revision']}")
    elif status == "update-available":
        current = payload.get("current_revision") or "none"
        print(f"update-available {payload['remote_revision']} (current {current})")
    elif status == "prepared":
        print(f"prepared {payload['revision']} {payload['checkout']}")
    elif status == "activated":
        print(f"activated {payload['revision']}")
    elif status == "no-change":
        print(f"already-active {payload['revision']}")
    elif status == "rolled-back":
        print(f"rolled-back {payload['current_revision']}")
    elif status == "status":
        print(f"current {payload.get('current_revision') or 'none'} previous {payload.get('previous_revision') or 'none'}")
    else:
        print(json.dumps(payload, sort_keys=True))


def prepare_bare(location: dict[str, Path]) -> None:
    ensure_private_directory(location["cache"])
    ensure_private_directory(location["checkouts"])
    bare = location["bare"]
    if bare.is_symlink():
        raise UpdateError(f"refusing a symlinked source cache: {bare}")
    if not bare.exists():
        command_text(["git", "clone", "--bare", "--no-tags", SOURCE_URL, str(bare)])
    elif not bare.is_dir():
        raise UpdateError(f"source cache is not a directory: {bare}")
    if bare.stat().st_uid != os.geteuid():
        raise UpdateError(f"source cache is not owned by the target user: {bare}")
    bare.chmod(0o700)
    try:
        # ``remote get-url`` applies URL rewrite rules, which would turn the
        # fixed HTTPS value into a test or local transport before validation.
        origin = git_text(bare, ["config", "--get", "remote.origin.url"])
    except UpdateError as error:
        raise UpdateError(f"private source cache has no usable origin: {bare}") from error
    if origin != SOURCE_URL:
        raise UpdateError(f"private source cache origin is not the fixed source: {bare}")
    git_text(bare, ["fetch", "--prune", "origin", f"+refs/heads/{SOURCE_BRANCH}:refs/remotes/origin/{SOURCE_BRANCH}"])


def checkout_is_valid(checkout: Path, revision: str) -> bool:
    if checkout.is_symlink() or not checkout.is_dir():
        return False
    try:
        head = validate_revision(git_text(checkout, ["rev-parse", "HEAD"]))
        clean = git_text(checkout, ["status", "--porcelain", "--untracked-files=all"]) == ""
    except UpdateError:
        return False
    return head == revision and clean


def prepare_checkout(home: Path, revision: str) -> Path:
    location = paths(home)
    prepare_bare(location)
    bare = location["bare"]
    try:
        git_text(bare, ["cat-file", "-e", f"{revision}^{{commit}}"])
    except UpdateError:
        # The branch fetch normally supplied this object.  The explicit check
        # makes it impossible to stage a SHA that was not obtained from Git.
        raise UpdateError(f"source revision is not present in the private cache: {revision}")

    checkout = location["checkouts"] / revision
    if checkout_is_valid(checkout, revision):
        return checkout
    if checkout.exists() or checkout.is_symlink():
        raise UpdateError(f"refusing an incomplete source checkout: {checkout}")

    pending = Path(tempfile.mkdtemp(prefix=f".{revision}.", dir=location["checkouts"]))
    shutil.rmtree(pending)
    try:
        # A regular shared clone has its own .git metadata, so renaming the
        # completed temporary directory cannot leave a stale worktree path in
        # the bare repository.  The objects remain private and are served by
        # the user-owned bare cache through Git's local alternates mechanism.
        git_text(None, ["clone", "--shared", "--no-tags", "--no-checkout", str(bare), str(pending)])
        git_text(pending, ["checkout", "--detach", revision])
        if not checkout_is_valid(pending, revision):
            raise UpdateError(f"private source checkout is not clean: {pending}")
        os.rename(pending, checkout)
    except Exception:
        if pending.exists() or pending.is_symlink():
            shutil.rmtree(pending)
        raise
    checkout.chmod(0o700)
    return checkout


def action_check(home: Path, as_json: bool) -> int:
    current, _ = current_state(home)
    revision = remote_revision()
    available = current != revision
    output(
        {
            "status": "update-available" if available else "up-to-date",
            "source_url": SOURCE_URL,
            "branch": SOURCE_BRANCH,
            "remote_revision": revision,
            "current_revision": current,
            "update_available": available,
        },
        as_json=as_json,
    )
    return 0


def action_prepare(home: Path, requested_revision: str | None, as_json: bool) -> int:
    current, _ = current_state(home)
    revision = validate_revision(requested_revision) if requested_revision else remote_revision()
    checkout = prepare_checkout(home, revision)
    status = "no-change" if current == revision else "prepared"
    output(
        {
            "status": status,
            "revision": revision,
            "checkout": str(checkout),
            "current_revision": current,
            "source_url": SOURCE_URL,
            "branch": SOURCE_BRANCH,
        },
        as_json=as_json,
    )
    return 0


def action_activate(home: Path, revision_text: str, as_json: bool) -> int:
    current, _ = current_state(home)
    revision = validate_revision(revision_text)
    if current == revision:
        output({"status": "no-change", "revision": revision}, as_json=as_json)
        return 0
    checkout = paths(home)["checkouts"] / revision
    if not checkout_is_valid(checkout, revision):
        raise UpdateError(f"prepared clean checkout not found; run prepare first: {revision}")
    stager = checkout / "install/arm64/stage-user-session.sh"
    if not stager.is_file() or stager.is_symlink():
        raise UpdateError(f"prepared source has no session stager: {stager}")
    result = subprocess.run(["bash", str(stager)], check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.returncode != 0:
        if result.stdout:
            sys.stderr.write(result.stdout)
        return result.returncode
    if not as_json and result.stdout:
        sys.stdout.write(result.stdout)
    output({"status": "activated", "revision": revision}, as_json=as_json)
    return 0


def action_rollback(home: Path, as_json: bool) -> int:
    current, previous = current_state(home)
    if not current or not previous:
        raise UpdateError("no previous Pi source release is available")
    release = paths(home)["data"] / "current"
    rollback = release / "install/arm64/rollback-user-session.sh"
    if not rollback.is_file() or rollback.is_symlink():
        raise UpdateError(f"active source release has no rollback helper: {rollback}")
    result = subprocess.run(["bash", str(rollback)], check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.returncode != 0:
        if result.stdout:
            sys.stderr.write(result.stdout)
        return result.returncode
    if not as_json and result.stdout:
        sys.stdout.write(result.stdout)
    new_current, _ = current_state(home)
    output({"status": "rolled-back", "current_revision": new_current}, as_json=as_json)
    return 0


def action_status(home: Path, as_json: bool) -> int:
    current, previous = current_state(home)
    output(
        {
            "status": "status",
            "current_revision": current,
            "previous_revision": previous,
        },
        as_json=as_json,
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="action", required=True)
    for name in ("check", "status", "rollback"):
        command = subparsers.add_parser(name)
        command.add_argument("--json", action="store_true", dest="as_json")
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--revision", help="full SHA already resolved by a prior check")
    prepare.add_argument("--json", action="store_true", dest="as_json")
    activate = subparsers.add_parser("activate")
    activate.add_argument("revision", help="full SHA returned by prepare")
    activate.add_argument("--json", action="store_true", dest="as_json")
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        home = require_user_home()
        if arguments.action == "check":
            return action_check(home, arguments.as_json)
        if arguments.action == "prepare":
            return action_prepare(home, arguments.revision, arguments.as_json)
        if arguments.action == "activate":
            return action_activate(home, arguments.revision, arguments.as_json)
        if arguments.action == "rollback":
            return action_rollback(home, arguments.as_json)
        if arguments.action == "status":
            return action_status(home, arguments.as_json)
        raise UpdateError(f"unknown action: {arguments.action}")
    except UpdateError as error:
        if getattr(arguments, "as_json", False):
            print(json.dumps({"status": "unavailable", "error": str(error)}, sort_keys=True))
        else:
            print(f"source update unavailable: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
