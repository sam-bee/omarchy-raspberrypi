#!/usr/bin/python3
"""Run Pi updates in a durable system service and report their persisted result."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import platform
import pwd
import stat
import subprocess
import sys
import time
import uuid

STATE_ROOT = Path("/var/lib/omarchy-pi-updates")
UNIT = "omarchy-pi-update.service"
RUNTIME_FILES = (
    "update.py", "update-source.py", "update-lib.py", "update_lib.py",
    "update-preflight.py", "update-verify.py", "update-migrations.py",
)
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


class UpdateError(RuntimeError):
    pass


def command(argv, **kwargs):
    return subprocess.run(argv, check=True, env=ENV, **kwargs)


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value, gid):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o640)
    os.chown(temporary, 0, gid)
    os.replace(temporary, path)


def active_release(account):
    data = Path(account.pw_dir) / ".local/share/omarchy-pi"
    current = data / "current"
    if not current.is_symlink():
        raise UpdateError("The account has no installed Omarchy Pi source release")
    target = current.readlink()
    import re
    if not re.fullmatch(r"releases/[0-9a-f]{40}", str(target)):
        raise UpdateError("The installed source pointer is invalid")
    release = data / target
    if release.is_symlink() or not release.is_dir():
        raise UpdateError("The installed source release is unavailable")
    if (release / ".omarchy-pi-source-commit").read_text().strip() != target.name:
        raise UpdateError("The installed source revision does not match its marker")
    return release


def require_pi():
    if platform.machine() != "aarch64":
        raise UpdateError("The Pi updater requires a native aarch64 system")
    if not Path("/run/systemd/system").is_dir():
        raise UpdateError("The Pi updater requires the running system service manager")


def root_directory(path, mode=0o755, gid=0):
    if path.is_symlink():
        raise UpdateError(f"Refusing a symlinked updater directory: {path}")
    if path.exists():
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise UpdateError(f"Updater directory has unexpected ownership or permissions: {path}")
    else:
        path.mkdir(mode=mode)
        os.chown(path, 0, gid)
        os.chmod(path, mode)


def launch():
    require_pi()
    if os.geteuid() != 0:
        raise UpdateError("Launching the system worker requires sudo")
    try:
        account = pwd.getpwuid(int(os.environ["SUDO_UID"]))
    except (KeyError, ValueError):
        raise UpdateError("Run omarchy update as the installed desktop user") from None
    if account.pw_uid == 0:
        raise UpdateError("Run omarchy update as the installed desktop user")
    release = active_release(account)
    root_directory(STATE_ROOT.parent)
    root_directory(STATE_ROOT)
    with (STATE_ROOT / "launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = command(["/usr/bin/systemctl", "show", UNIT, "--property=ActiveState", "--value"], capture_output=True, text=True).stdout.strip()
        if state in {"active", "activating", "deactivating", "reloading"}:
            raise UpdateError("An update is already running; use omarchy update --status")
        job = STATE_ROOT / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8])
        root_directory(job, 0o750, account.pw_gid)
        runtime = job / "runtime"
        root_directory(runtime, 0o755)
        source = Path(__file__).resolve().parent
        for name in RUNTIME_FILES:
            original = source / name
            if original.is_symlink() or not original.is_file():
                raise UpdateError(f"Updater component is missing: {name}")
            destination = runtime / name
            destination.write_bytes(original.read_bytes())
            destination.chmod(0o644)
        request = {"username": account.pw_name, "uid": account.pw_uid, "gid": account.pw_gid, "home": account.pw_dir, "old_release": str(release)}
        write_json(job / "request.json", request, account.pw_gid)
        write_json(job / "result.json", {"status": "queued", "phase": "starting", "job": str(job)}, account.pw_gid)
        log = job / "update.log"
        log.touch(mode=0o640)
        log.chmod(0o640)
        os.chown(log, 0, account.pw_gid)
        pointer = STATE_ROOT / ("latest-" + str(account.pw_uid))
        temporary = pointer.with_name(pointer.name + ".tmp")
        temporary.write_text(job.name + "\n")
        temporary.chmod(0o644)
        os.replace(temporary, pointer)
        subprocess.run(["/usr/bin/systemctl", "reset-failed", UNIT], env=ENV, capture_output=True, check=False)
        try:
            command([
                "/usr/bin/systemd-run", "--quiet", "--collect", "--unit=" + UNIT,
                "--property=Type=exec", "--property=UMask=0027",
                "--property=StandardOutput=append:" + str(log),
                "--property=StandardError=append:" + str(log),
                "/usr/bin/python3", "-I", str(runtime / "update.py"), "--worker", str(job),
            ])
        except Exception:
            write_json(job / "result.json", {"status": "failed", "phase": "starting", "error": "Could not start the system update worker", "job": str(job)}, account.pw_gid)
            raise
        print(json.dumps({"job": str(job), "status": "queued"}))


def user_command(account, argv, **kwargs):
    return command([
        "/usr/bin/runuser", "-u", account.pw_name, "--", "/usr/bin/env", "-i",
        "HOME=" + account.pw_dir, "USER=" + account.pw_name, "LOGNAME=" + account.pw_name,
        "PATH=/usr/bin:/bin", "LANG=C.UTF-8", "XDG_RUNTIME_DIR=/run/user/" + str(account.pw_uid),
        *argv,
    ], **kwargs)


def worker(job):
    require_pi()
    if os.geteuid() != 0 or job.parent != STATE_ROOT or job.is_symlink():
        raise UpdateError("Invalid system worker invocation")
    info = job.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise UpdateError("The update job is not root-owned")
    request = read_json(job / "request.json")
    account = pwd.getpwuid(request["uid"])
    if account.pw_name != request["username"] or account.pw_dir != request["home"] or account.pw_gid != request["gid"]:
        raise UpdateError("The installed account changed before the update started")
    runtime = job / "runtime"
    result = {"status": "running", "phase": "preflight", "job": str(job), "started_at": time.time(), "source_before": Path(request["old_release"]).name}

    def phase(name):
        result["phase"] = name
        write_json(job / "result.json", result, account.pw_gid)
        print("\n== " + name + " ==", flush=True)

    def root_helper(name, *args):
        command(["/usr/bin/python3", str(runtime / name), *map(str, args)])

    def source(*args):
        try:
            response = user_command(account, ["/usr/bin/python3", str(runtime / "update-source.py"), *args], capture_output=True, text=True)
        except subprocess.CalledProcessError as error:
            print(error.stdout or "", end="", flush=True)
            print(error.stderr or "", end="", file=sys.stderr, flush=True)
            raise
        return json.loads(response.stdout)

    def record_packages(label):
        package_text = command(["/usr/bin/pacman", "-Q"], capture_output=True, text=True).stdout
        write_json(job / ("packages-" + label + ".json"), package_text.splitlines(), account.pw_gid)

    try:
        with (STATE_ROOT / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if str(active_release(account)) != request["old_release"]:
                raise UpdateError("The active source changed before the update started")
            phase("preflight")
            root_helper("update-preflight.py", "--home", account.pw_dir, "--baseline", job / "baseline.json")
            root_helper("update-verify.py", "--baseline", job / "baseline.json", "--home", account.pw_dir)
            record_packages("before")
            phase("prepare-source")
            prepared = source("prepare", "--json")
            revision, checkout = prepared["revision"], prepared["checkout"]
            result["source_candidate"] = revision
            migration_args = ["--old-release", request["old_release"], "--new-release", checkout]
            root_helper("update-preflight.py", "--home", account.pw_dir, *migration_args)
            phase("review-migrations")
            user_command(account, ["/usr/bin/python3", str(runtime / "update-migrations.py"), *migration_args, "--check"])
            phase("prepare-packages")
            # Download and verify the complete transaction before the explicit
            # EEPROM gate. Apply the same synchronized databases with -Su.
            command(["/usr/bin/env", "OMARCHY_UPDATE_PACMAN=1", "/usr/bin/pacman", "-Syuw", "--noconfirm"])
            pending = command(["/usr/bin/pacman", "-Sup", "--print-format", "%n %v"], capture_output=True, text=True).stdout
            package_plan = job / "package-plan.txt"
            package_plan.write_text(pending)
            package_plan.chmod(0o640)
            os.chown(package_plan, 0, account.pw_gid)
            packages = {line.split()[0] for line in pending.splitlines() if line.strip()}
            if any("eeprom" in name.lower() for name in packages):
                raise UpdateError("The transaction includes EEPROM packages; review those separately before updating")
            result["planned_packages"] = sorted(packages)
            phase("update-packages")
            command(["/usr/bin/env", "OMARCHY_UPDATE_PACMAN=1", "/usr/bin/pacman", "-Su", "--noconfirm"])
            phase("verify-system")
            root_helper("update-verify.py", "--baseline", job / "baseline.json", "--home", account.pw_dir)
            phase("migrations")
            user_command(account, ["/usr/bin/python3", str(runtime / "update-migrations.py"), *migration_args, "--run"])
            phase("activate-source")
            source("activate", revision, "--json")
            phase("final-verification")
            root_helper("update-verify.py", "--baseline", job / "baseline.json", "--home", account.pw_dir)
            result.update(status="complete", source_after=active_release(account).name, finished_at=time.time(), reboot_required=True)
            print("Update complete. Reboot when ready, then check SSH and the desktop. Source rollback does not undo package updates.", flush=True)
    except Exception as error:
        result.update(status="failed", error=str(error), finished_at=time.time())
        print("Update stopped: " + str(error), file=sys.stderr, flush=True)
        print("The package transaction may have changed the system. Inspect the log before retrying; USB recovery remains available.", file=sys.stderr, flush=True)
    finally:
        try:
            record_packages("after")
        except Exception as error:
            result.update(status="failed", error=result.get("error", "Could not record the final installed package set: " + str(error)))
        write_json(job / "result.json", result, account.pw_gid)
    return 0 if result["status"] == "complete" else 1


def latest_job():
    pointer = STATE_ROOT / ("latest-" + str(os.getuid()))
    try:
        name = pointer.read_text().strip()
    except FileNotFoundError:
        raise UpdateError("No Pi update has been started by this account") from None
    if Path(name).name != name or not name:
        raise UpdateError("The saved update job is invalid")
    return STATE_ROOT / name


def report(job, watch):
    offset = 0
    while True:
        result = read_json(job / "result.json")
        if result["status"] in {"queued", "running"}:
            active = subprocess.run(["/usr/bin/systemctl", "is-active", "--quiet", UNIT], env=ENV).returncode == 0
            if not active and time.time() - result.get("started_at", job.stat().st_mtime) > 15:
                result = {**result, "status": "failed", "error": "The worker stopped without a completion record. Inspect the log before retrying."}
        log = job / "update.log"
        if log.exists():
            with log.open() as stream:
                stream.seek(offset)
                text = stream.read()
                offset = stream.tell()
            if text:
                print(text, end="", flush=True)
        if not watch or result["status"] in {"complete", "failed"}:
            print(f"\nUpdate {result['status']}; phase: {result['phase']}; log: {log}")
            if result.get("error"):
                print(result["error"], file=sys.stderr)
            return 1 if result["status"] == "failed" else 0
        time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-y", action="store_true", help="confirm a complete Arch Linux ARM and downstream source update")
    parser.add_argument("--status", action="store_true", help="show the last update result and log")
    parser.add_argument("--watch", action="store_true", help="follow the last update; disconnecting leaves it running")
    parser.add_argument("--launch", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.worker:
            return worker(args.worker)
        if args.launch:
            launch()
            return 0
        if os.geteuid() == 0:
            raise UpdateError("Run omarchy update as the installed desktop user without sudo")
        if args.status or args.watch:
            return report(latest_job(), args.watch)
        require_pi()
        if not args.y:
            reply = input("Update Arch Linux ARM packages and Omarchy Pi source? [y/N] ").strip().lower()
            if reply not in {"y", "yes"}:
                return 0
        print("Starting a system update job. Closing this terminal will leave it running.", flush=True)
        completed = subprocess.run(["/usr/bin/sudo", "/usr/bin/python3", "-I", str(Path(__file__).resolve()), "--launch"], check=True, stdout=subprocess.PIPE, text=True)
        job = Path(json.loads(completed.stdout)["job"])
        return report(job, True)
    except KeyboardInterrupt:
        print("\nDetached. Use omarchy update --watch to reconnect; the system worker continues.")
        return 130
    except (UpdateError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        if args.worker and os.geteuid() == 0 and args.worker.parent == STATE_ROOT and not args.worker.is_symlink():
            try:
                info = args.worker.stat()
                if info.st_uid == 0 and not info.st_mode & 0o022:
                    write_json(args.worker / "result.json", {"job": str(args.worker), "status": "failed", "phase": "worker-start", "error": str(error)}, info.st_gid)
            except OSError:
                pass
        print("Pi update: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
