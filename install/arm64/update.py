#!/usr/bin/python3
"""Run Pi updates in a durable system service and report their persisted result."""

from __future__ import annotations

import argparse
from dataclasses import replace
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import platform
import pwd
import shutil
import stat
import subprocess
import sys
import time
import uuid

try:
    import update_packages
except ModuleNotFoundError:  # The focused tests load this file by pathname.
    _package_spec = importlib.util.spec_from_file_location(
        "omarchy_pi_update_packages", Path(__file__).with_name("update_packages.py")
    )
    if _package_spec is None or _package_spec.loader is None:
        raise
    update_packages = importlib.util.module_from_spec(_package_spec)
    sys.modules[_package_spec.name] = update_packages
    _package_spec.loader.exec_module(update_packages)

STATE_ROOT = Path("/var/lib/omarchy-pi-updates")
PACKAGE_ROLLBACK_ROOT = Path("/usr/share/omarchy-pi/rollback")
PACKAGED_RUNTIME_ROOT = Path("/usr/share/omarchy")
UNIT = "omarchy-pi-update.service"
RUNTIME_FILES = (
    "update.py", "update-source.py", "update-lib.py", "update_lib.py", "update_packages.py",
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


def require_packaged_runtime():
    require_pi()
    channel = update_packages.package_provenance()
    if channel is None:
        raise UpdateError("The installed /usr/share/omarchy tree is not owned by a compatible Omarchy package pair")
    return channel


def _prepare_packaged_command(argv, *, home):
    environment = _prepare_packaged_environment(home)
    try:
        result = subprocess.run(argv, check=True, env=environment, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "no diagnostic output").strip()
        raise UpdateError(f"Could not prepare the Omarchy package candidate: {detail}") from error
    return result


def _prepare_packaged_environment(home):
    account = pwd.getpwuid(os.getuid())
    return {
        "HOME": str(home),
        "USER": account.pw_name,
        "LOGNAME": account.pw_name,
        **ENV,
    }


def _prepare_packaged_source_archive(checkout, revision, destination, *, home):
    """Create the same deterministic Git archive used by the package builder."""

    environment = _prepare_packaged_environment(home)
    try:
        with Path(destination).open("wb") as stream:
            result = subprocess.run(
                ["/usr/bin/git", "-C", str(checkout), "archive", "--format=tar", revision],
                check=True, env=environment, stdout=stream, stderr=subprocess.PIPE,
            )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or b"no diagnostic output").decode(errors="replace").strip()
        raise UpdateError(f"Could not bind the package source archive: {detail}") from error
    except OSError as error:
        raise UpdateError("Could not create the package source archive") from error


def prepare_packaged_candidate(explicit_root=None):
    """Fetch/build a candidate as the desktop user before sudo is involved."""

    require_packaged_runtime()
    account = pwd.getpwuid(os.getuid())
    home = Path(account.pw_dir)
    if explicit_root is not None:
        candidate = update_packages.load_candidate(explicit_root, require_previous=False)
        return candidate.root

    runtime = PACKAGED_RUNTIME_ROOT
    source_updater = runtime / "install/arm64/update-source.py"
    if source_updater.is_symlink() or not source_updater.is_file():
        raise UpdateError(f"The packaged runtime has no pinned ARM source updater: {source_updater}")
    prepared = _prepare_packaged_command(
        ["/usr/bin/python3", str(source_updater), "prepare", "--json"], home=home
    )
    try:
        source_info = json.loads(prepared.stdout)
        revision = source_info["revision"]
        checkout = Path(source_info["checkout"]).resolve()
    except (KeyError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise UpdateError("The pinned source updater returned invalid candidate metadata") from error
    if not isinstance(revision, str) or not update_packages.REVISION.fullmatch(revision):
        raise UpdateError("The pinned source updater returned an invalid source revision")
    checkout_root = (home / ".cache/omarchy-pi/checkouts").resolve()
    try:
        checkout.relative_to(checkout_root)
    except ValueError as error:
        raise UpdateError("The pinned source checkout is outside the private source cache") from error
    builder = checkout / "install/arm64/build-runtime-packages.py"
    if builder.is_symlink() or not builder.is_file():
        raise UpdateError(f"The pinned source checkout has no runtime package builder: {builder}")

    candidate_root = home / ".cache/omarchy-pi/runtime-candidates" / revision
    if candidate_root.exists() or candidate_root.is_symlink():
        candidate = update_packages.load_candidate(candidate_root, require_previous=False)
        if candidate.source_revision != revision:
            raise UpdateError("The cached package candidate source revision does not match the checkout")
        return candidate.root
    candidate_root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    candidate_root.parent.chmod(0o700)
    _prepare_packaged_command(
        [
            "/usr/bin/python3", str(builder), "--source-checkout", str(checkout),
            "--source-revision", revision, "--output", str(candidate_root), "--json",
        ],
        home=home,
    )
    source_archive = candidate_root / "source.tar"
    _prepare_packaged_source_archive(checkout, revision, source_archive, home=home)
    pair_document = update_packages._pair_document(candidate_root)
    if pair_document is None:
        raise UpdateError("The runtime package builder did not emit a pair manifest")
    source = pair_document.get("source")
    if not isinstance(source, dict) or source.get("archive_sha256") != update_packages._digest(source_archive):
        raise UpdateError("The package source archive differs from the builder's recorded provenance")
    source["archive"] = source_archive.name
    pair_document["source"] = source
    (candidate_root / "candidate.json").write_text(
        json.dumps(pair_document, sort_keys=True, indent=2) + "\n", encoding="utf-8",
    )
    # The package builder deliberately emits only package artifacts.  Retain a
    # clean user-readable source tree beside them so the root worker can bind
    # the migration review to the exact commit without executing user source.
    shutil.copytree(checkout, candidate_root / "source", symlinks=True, ignore=shutil.ignore_patterns(".git"))
    source_marker = candidate_root / "source/.omarchy-pi-source-commit"
    if source_marker.is_symlink():
        raise UpdateError("The pinned source checkout contains a symlinked provenance marker")
    source_marker.write_text(revision + "\n", encoding="utf-8")
    candidate = update_packages.load_candidate(candidate_root, require_previous=False)
    if candidate.source_revision != revision:
        raise UpdateError("The built package candidate source revision does not match the checkout")
    return candidate.root


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


def _copy_candidate_file(candidate_root, snapshot_root, source):
    source = Path(source)
    try:
        relative = source.relative_to(candidate_root)
    except ValueError as error:
        raise UpdateError(f"The package candidate file escapes its root: {source}") from error
    destination = snapshot_root / relative
    destination.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if source.is_symlink() or not source.is_file():
        raise UpdateError(f"The package candidate file is not regular: {source}")
    shutil.copy2(source, destination)


def _snapshot_candidate(candidate, destination):
    """Copy only the validated candidate inputs into the root-owned job."""

    destination = Path(destination)
    destination.mkdir(mode=0o750)
    candidate_root = Path(candidate.root)
    _copy_candidate_file(candidate_root, destination, candidate.manifest)
    source_relative = Path(candidate.source_tree).relative_to(candidate_root)
    source_destination = destination / source_relative
    source_destination.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    shutil.copytree(candidate.source_tree, source_destination, symlinks=True)
    source_archive = getattr(candidate, "source_archive", None)
    if source_archive is not None:
        _copy_candidate_file(candidate_root, destination, source_archive)
    for package in (*candidate.packages, *candidate.previous_packages):
        _copy_candidate_file(candidate_root, destination, package.archive)
        if package.signature_file is not None:
            _copy_candidate_file(candidate_root, destination, package.signature_file)


def _make_candidate_snapshot_readable(root, gid):
    """Expose the root-owned review snapshot to the desktop worker account."""

    root = Path(root)
    for path in (root, *root.rglob("*")):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            # The candidate loader has already checked that source symlinks
            # resolve inside the snapshot. Do not follow them while changing
            # ownership or permissions.
            continue
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise UpdateError(f"The package candidate contains an unsupported file: {path}")
        os.chown(path, 0, gid, follow_symlinks=False)
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            os.chmod(path, mode | 0o050)
        else:
            os.chmod(path, mode | 0o040)


def _snapshot_previous_package_pair(packages, destination, gid):
    """Persist a validated pre-update pair below the durable job snapshot.

    The active rollback directory is replaced after a successful transaction.
    A job-specific copy must therefore be complete before pacman runs and must
    not share any archive paths with that directory.  Publish the copy through
    a temporary sibling, then load it again through the normal rollback
    validator so both the manifest and archive digests are checked before the
    worker proceeds.
    """

    destination = Path(destination)
    if destination.is_symlink() or destination.exists():
        raise UpdateError(f"The previous package snapshot already exists: {destination}")
    parent = destination.parent
    if parent.is_symlink() or not parent.is_dir():
        raise UpdateError(f"The package snapshot parent is unavailable: {parent}")
    staging = parent / (".previous-" + uuid.uuid4().hex)
    staging.mkdir(mode=0o750)
    os.chown(staging, 0, gid)
    os.chmod(staging, 0o750)
    try:
        records = []
        for package in packages:
            archive = Path(package.archive)
            if archive.is_symlink() or not archive.is_file():
                raise UpdateError(f"The retained package archive is unavailable: {archive}")
            archive_destination = staging / archive.name
            shutil.copyfile(archive, archive_destination)
            archive_destination.chmod(0o640)
            os.chown(archive_destination, 0, gid)
            with archive_destination.open("rb") as stream:
                os.fsync(stream.fileno())
            if update_packages._digest(archive_destination) != package.sha256:
                raise UpdateError(f"The retained package archive changed while being copied: {archive}")
            record = {
                "name": package.name,
                "version": package.version,
                "architecture": package.architecture,
                "filename": archive_destination.name,
                "sha256": package.sha256,
                "signature": package.signature,
            }
            if package.signature == "required":
                signature = package.signature_file
                if signature is None or signature.is_symlink() or not signature.is_file():
                    raise UpdateError(f"The retained package signature is unavailable: {signature}")
                signature_destination = staging / signature.name
                shutil.copyfile(signature, signature_destination)
                signature_destination.chmod(0o640)
                os.chown(signature_destination, 0, gid)
                with signature_destination.open("rb") as stream:
                    os.fsync(stream.fileno())
                if update_packages._digest(signature_destination) != update_packages._digest(signature):
                    raise UpdateError(f"The retained package signature changed while being copied: {signature}")
                record["signature_file"] = signature_destination.name
            records.append(record)
        if not records:
            raise UpdateError("The installed package pair is empty")
        write_json(staging / "manifest.json", {
            "schema_version": 1,
            "architecture": "aarch64",
            "version": records[0]["version"],
            "packages": records,
        }, gid)
        retained = update_packages.load_installed_rollback(
            tuple(record["name"] for record in records), roots=(staging,)
        )
        if {package.sha256 for package in retained} != {record["sha256"] for record in records}:
            raise UpdateError("The previous package snapshot failed checksum validation")
        os.replace(staging, destination)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        # Read back the published path as the worker will later consume it.
        return update_packages.load_installed_rollback(
            tuple(record["name"] for record in records), roots=(destination,)
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def launch(packaged=False, candidate_root=None):
    channel = require_packaged_runtime() if packaged else None
    if os.geteuid() != 0:
        raise UpdateError("Launching the system worker requires sudo")
    try:
        account = pwd.getpwuid(int(os.environ["SUDO_UID"]))
    except (KeyError, ValueError):
        raise UpdateError("Run omarchy update as the installed desktop user") from None
    if account.pw_uid == 0:
        raise UpdateError("Run omarchy update as the installed desktop user")
    release = active_release(account) if not packaged else PACKAGED_RUNTIME_ROOT
    candidate = update_packages.load_candidate(candidate_root, require_previous=False) if packaged else None
    root_directory(STATE_ROOT.parent)
    root_directory(STATE_ROOT)
    with (STATE_ROOT / "launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = command(["/usr/bin/systemctl", "show", UNIT, "--property=ActiveState", "--value"], capture_output=True, text=True).stdout.strip()
        if state in {"active", "activating", "deactivating", "reloading"}:
            raise UpdateError("An update is already running; use omarchy update --status")
        job = STATE_ROOT / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8])
        root_directory(job, 0o750, account.pw_gid)
        if packaged:
            # The candidate was prepared in the desktop user's cache.  Take a
            # root-owned snapshot before creating the durable request so a
            # later cache edit cannot change the pair or migration provenance
            # while the PID 1 worker is waiting to run.
            candidate_snapshot = job / "candidate"
            _snapshot_candidate(candidate, candidate_snapshot)
            _make_candidate_snapshot_readable(candidate_snapshot, account.pw_gid)
            candidate = update_packages.load_candidate(candidate_snapshot, require_previous=False)
        runtime = job / "runtime"
        root_directory(runtime, 0o755)
        source = PACKAGED_RUNTIME_ROOT / "install/arm64" if packaged else Path(__file__).resolve().parent
        for name in RUNTIME_FILES:
            original = source / name
            if original.is_symlink() or not original.is_file():
                raise UpdateError(f"Updater component is missing: {name}")
            destination = runtime / name
            destination.write_bytes(original.read_bytes())
            destination.chmod(0o644)
        request = {
            "username": account.pw_name,
            "uid": account.pw_uid,
            "gid": account.pw_gid,
            "home": account.pw_dir,
            "old_release": str(release),
            "mode": "packaged" if packaged else "source",
        }
        if packaged:
            request["candidate_root"] = str(candidate.root)
            request["candidate_manifest"] = str(candidate.manifest)
            request["channel"] = channel
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


def _copy_previous_migrations(old_release, destination):
    source = Path(old_release) / "migrations"
    if not source.is_dir() or source.is_symlink():
        raise UpdateError("The installed Omarchy runtime has no readable migrations directory")
    target = Path(destination) / "migrations"
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    shutil.copytree(source, target, symlinks=False)
    return target.parent


def _local_pacman_config(job, account, candidate):
    """Create a per-job config when the reviewed candidate has unsigned files."""

    if not update_packages.needs_optional_local_signature(candidate):
        return None
    try:
        original = Path("/etc/pacman.conf").read_text(encoding="utf-8")
    except OSError as error:
        raise UpdateError("Could not read the target pacman configuration for the local candidate") from error
    lines = original.splitlines()
    try:
        options = next(index for index, line in enumerate(lines) if line.strip().lower() == "[options]")
    except StopIteration as error:
        raise UpdateError("The target pacman configuration has no [options] section") from error
    end = next((index for index in range(options + 1, len(lines)) if lines[index].strip().startswith("[")), len(lines))
    lines.insert(end, "LocalFileSigLevel = Optional")
    config = Path(job) / "pacman-local.conf"
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config.chmod(0o640)
    os.chown(config, 0, account.pw_gid)
    return config


def _retain_installed_package_pair(candidate):
    """Publish the just-installed pair as the next offline rollback point.

    Each publication uses unique archive names.  That means an interruption
    while moving the new files cannot overwrite archives still referenced by
    the old manifest; the manifest remains the one atomic publication record.
    """

    parent = PACKAGE_ROLLBACK_ROOT.parent
    root_directory(parent)
    root_directory(PACKAGE_ROLLBACK_ROOT)
    publication = uuid.uuid4().hex
    staged = PACKAGE_ROLLBACK_ROOT / (".pair-" + publication)
    staged.mkdir(mode=0o750)
    os.chown(staged, 0, 0)
    try:
        records = []
        for package in candidate.packages:
            filename = publication + "-" + package.archive.name
            destination = staged / filename
            shutil.copyfile(package.archive, destination)
            destination.chmod(0o640)
            os.chown(destination, 0, 0)
            with destination.open("rb") as stream:
                os.fsync(stream.fileno())
            record = {
                "name": package.name,
                "version": package.version,
                "architecture": package.architecture,
                "filename": filename,
                "sha256": package.sha256,
                "signature": package.signature,
            }
            if package.signature == "required":
                signature = package.signature_file or package.archive.with_name(package.archive.name + ".sig")
                if signature.is_symlink() or not signature.is_file():
                    raise UpdateError(f"The installed package signature is unavailable: {signature}")
                signature_destination = staged / (filename + ".sig")
                shutil.copyfile(signature, signature_destination)
                signature_destination.chmod(0o640)
                os.chown(signature_destination, 0, 0)
                with signature_destination.open("rb") as stream:
                    os.fsync(stream.fileno())
                record["signature_file"] = signature_destination.name
            records.append(record)
        manifest = {
            "schema_version": 1,
            "architecture": "aarch64",
            "version": candidate.packages[0].version,
            "packages": records,
        }
        source_revision = getattr(candidate, "source_revision", None)
        if isinstance(source_revision, str) and update_packages.REVISION.fullmatch(source_revision):
            manifest["source_revision"] = source_revision
        manifest_tmp = staged / "manifest.json"
        with manifest_tmp.open("w", encoding="utf-8") as stream:
            json.dump(manifest, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        manifest_tmp.chmod(0o640)
        os.chown(manifest_tmp, 0, 0)
        # Archives are installed first; the manifest is the final publication
        # record consumed by the next worker's checksum validation.
        for path in staged.iterdir():
            if path.name == "manifest.json":
                continue
            os.replace(path, PACKAGE_ROLLBACK_ROOT / path.name)
        directory_fd = os.open(PACKAGE_ROLLBACK_ROOT, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.replace(manifest_tmp, PACKAGE_ROLLBACK_ROOT / "manifest.json")
        directory_fd = os.open(PACKAGE_ROLLBACK_ROOT, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        active_names = {record["filename"] for record in records}
        active_names.update(record["signature_file"] for record in records if "signature_file" in record)
        for path in PACKAGE_ROLLBACK_ROOT.iterdir():
            if path.name == "manifest.json" or path.name in active_names or path.is_dir():
                continue
            try:
                path.unlink()
            except OSError:
                pass
    except Exception:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    shutil.rmtree(staged, ignore_errors=True)
    return PACKAGE_ROLLBACK_ROOT / "manifest.json"


def _package_worker(job, request, account, runtime, result, phase, root_helper, user_command, record_packages):
    """Run the packaged candidate path inside the same durable worker."""

    candidate = update_packages.load_candidate(request["candidate_root"], require_previous=False)
    if request.get("candidate_manifest") != str(candidate.manifest):
        raise UpdateError("The package candidate changed after launch")
    if request.get("channel") != candidate.channel:
        raise UpdateError("The package candidate channel changed after launch")
    if update_packages.package_provenance() != candidate.channel:
        raise UpdateError("The installed Omarchy package pair changed after launch")
    rollback = update_packages.load_installed_rollback(candidate.package_names)
    previous_snapshot = Path(job) / "previous"
    rollback = _snapshot_previous_package_pair(rollback, previous_snapshot, account.pw_gid)
    candidate = replace(candidate, previous_packages=rollback)
    result["rollback_before"] = str(previous_snapshot / "manifest.json")
    result["candidate_source"] = candidate.source_revision
    result["candidate_source_sha256"] = candidate.source_sha256
    result["rollback_packages"] = [
        {"name": package.name, "version": package.version, "archive": str(package.archive), "sha256": package.sha256}
        for package in candidate.previous_packages
    ]
    old_release = Path(request["old_release"])
    # Reuse the existing recipe-drift gate before touching the package pair.
    # The candidate source tree is the reviewed provenance for the locally
    # built archives; it is never executed as the update mechanism.
    root_helper("update-preflight.py", "--home", account.pw_dir,
                "--old-release", old_release, "--new-release", candidate.source_tree)
    old_snapshot = Path(job) / "old-runtime"
    _copy_previous_migrations(old_release, old_snapshot)
    _make_candidate_snapshot_readable(old_snapshot, account.pw_gid)
    migration_args = ["--old-release", old_snapshot, "--new-release", candidate.source_tree,
                      "--policy", candidate.source_tree / "install/arm64/migrations.allowlist"]
    phase("review-migrations")
    user_command(account, ["/usr/bin/python3", str(runtime / "update-migrations.py"), *map(str, migration_args), "--check"])
    phase("prepare-packages")
    # Keep the existing ARM package review for the Arch Linux ARM base.  The
    # Omarchy pair is deliberately absent from repository resolution: it is
    # supplied only by the reviewed local candidate below.
    command(["/usr/bin/env", "OMARCHY_UPDATE_PACMAN=1", "/usr/bin/pacman", "-Syuw", "--noconfirm"])
    pending = command(["/usr/bin/pacman", "-Sup", "--print-format", "%n %v"], capture_output=True, text=True).stdout
    package_plan = Path(job) / "package-plan.txt"
    package_plan.write_text(pending)
    package_plan.chmod(0o640)
    os.chown(package_plan, 0, account.pw_gid)
    repository_packages = {line.split()[0] for line in pending.splitlines() if line.strip()}
    if repository_packages & set(update_packages.PACKAGE_PAIRS["stable"] + update_packages.PACKAGE_PAIRS["dev"]):
        raise UpdateError("the repository plan contains Omarchy runtime packages; use the reviewed local candidate pair")
    if any("eeprom" in name.lower() for name in repository_packages):
        raise UpdateError("The transaction includes EEPROM packages; review those separately before updating")
    installed_text = command(["/usr/bin/pacman", "-Q", *candidate.package_names], capture_output=True, text=True).stdout
    installed_versions = {
        fields[0]: fields[1]
        for fields in (line.split() for line in installed_text.splitlines())
        if len(fields) >= 2 and fields[0] in candidate.package_names
    }
    result["package_actions"] = update_packages.candidate_actions(candidate, installed_versions)
    same_source = update_packages.pair_is_current(
        candidate, installed_versions, update_packages.installed_source_revision(), rollback,
    )
    result["omarchy_pair_action"] = "current" if same_source else "replace"
    result["planned_packages"] = sorted(repository_packages | set(package.name for package in candidate.packages))
    phase("update-packages")
    if same_source:
        result["local_file_signature_policy"] = "unchanged"
    else:
        pacman_config = _local_pacman_config(job, account, candidate)
        transaction = update_packages.local_transaction_command(candidate, force=True)
        if pacman_config is not None:
            transaction[1:1] = ["--config", str(pacman_config)]
            result["local_file_signature_policy"] = "Optional"
        else:
            result["local_file_signature_policy"] = "Required"
        command(["/usr/bin/env", "OMARCHY_UPDATE_PACMAN=1", *transaction])
    command(["/usr/bin/env", "OMARCHY_UPDATE_PACMAN=1", "/usr/bin/pacman", "-Su", "--noconfirm"])
    phase("verify-system")
    root_helper("update-verify.py", "--baseline", job / "baseline.json", "--home", account.pw_dir)
    phase("migrations")
    installed_migration_args = ["--old-release", old_snapshot, "--new-release", "/usr/share/omarchy",
                                "--policy", candidate.source_tree / "install/arm64/migrations.allowlist"]
    user_command(account, ["/usr/bin/python3", str(runtime / "update-migrations.py"), *map(str, installed_migration_args), "--run"])
    phase("final-verification")
    root_helper("update-verify.py", "--baseline", job / "baseline.json", "--home", account.pw_dir)
    rollback_manifest = _retain_installed_package_pair(candidate)
    result["rollback_after"] = str(rollback_manifest)
    result.update(status="complete", source_after=candidate.source_revision, finished_at=time.time(), reboot_required=True)
    print("Update complete. Reboot when ready, then check SSH and the desktop. Previous package archives remain the rollback boundary.", flush=True)


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
            if request.get("mode", "source") == "source" and str(active_release(account)) != request["old_release"]:
                raise UpdateError("The active source changed before the update started")
            phase("preflight")
            root_helper("update-preflight.py", "--home", account.pw_dir, "--baseline", job / "baseline.json")
            root_helper("update-verify.py", "--baseline", job / "baseline.json", "--home", account.pw_dir)
            record_packages("before")
            if request.get("mode", "source") == "packaged":
                _package_worker(job, request, account, runtime, result, phase, root_helper, user_command, record_packages)
                return 0 if result["status"] == "complete" else 1
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
    parser.add_argument("-y", action="store_true", help="confirm a complete Arch Linux ARM and Omarchy update")
    parser.add_argument("--status", action="store_true", help="show the last update result and log")
    parser.add_argument("--watch", action="store_true", help="follow the last update; disconnecting leaves it running")
    parser.add_argument("--packaged", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--candidate", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--launch", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.worker:
            return worker(args.worker)
        if args.launch:
            launch(args.packaged, args.candidate)
            return 0
        if os.geteuid() == 0:
            raise UpdateError("Run omarchy update as the installed desktop user without sudo")
        if args.status or args.watch:
            return report(latest_job(), args.watch)
        candidate_root = None
        if args.packaged:
            require_packaged_runtime()
        else:
            require_pi()
        candidate_root = prepare_packaged_candidate(args.candidate) if args.packaged else None
        if not args.y:
            prompt = "Update Arch Linux ARM packages and the Omarchy pair from the reviewed local candidate? [y/N] " if args.packaged else "Update Arch Linux ARM packages and Omarchy Pi source? [y/N] "
            reply = input(prompt).strip().lower()
            if reply not in {"y", "yes"}:
                return 0
        print("Starting a system update job. Closing this terminal will leave it running.", flush=True)
        launch_args = ["--launch"] + (["--packaged"] if args.packaged else [])
        if candidate_root is not None:
            launch_args += ["--candidate", str(candidate_root)]
        completed = subprocess.run(["/usr/bin/sudo", "/usr/bin/python3", "-I", str(Path(__file__).resolve()), *launch_args], check=True, stdout=subprocess.PIPE, text=True)
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
