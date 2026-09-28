#!/usr/bin/python3
"""The small, session-independent controller for the Pi disk installer.

The installer is deliberately split into three processes:

* ``omarchy-pi-install`` is an ordinary terminal client;
* ``installer-control`` is the only privileged entry point and accepts one
  JSON request on standard input; and
* the system service runs the queued job without inheriting a terminal.

Only the request file contains the settings secrets.  It lives below ``/run``
and is removed when the worker exits.  Durable state and logs contain only
non-secret values.  The storage and target operations are supplied by the
small ``disk_install`` and ``installed_target`` modules; keeping those calls
here makes the destructive boundary straightforward to test.
"""

from __future__ import annotations

import contextlib
import datetime as _datetime
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Iterator, Mapping
import urllib.error
import urllib.request
import uuid


STATE_ROOT = Path("/var/lib/omarchy-pi/installer")
RUNTIME_ROOT = Path("/run/omarchy-pi-installer")
STATE_FILE_NAME = "state.json"
CONTROL_LOCK_NAME = "control.lock"
WORKER_LOCK_NAME = "worker.lock"
REQUEST_FILE_NAME = "request.json"
SERVICE_NAME = "omarchy-pi-install.service"
CONTROL_PATH = "/usr/local/libexec/omarchy-pi/installer-control"
MAX_INPUT_BYTES = 512 * 1024
POLL_SECONDS = 2.0
MIB = 1024 * 1024
LUKS_OVERHEAD_BYTES = 16 * MIB

TERMINAL_STATES = frozenset(("complete", "failed", "interrupted"))
ACTIVE_STATES = frozenset(("queued", "running"))
RESTART_CONFIRMATION = "RESTART FROM SCRATCH"
# setup-mise.sh downloads this fixed ARM64 release from GitHub during target
# user setup.  Probe the actual release URL before erasing a target, without
# carrying any user settings, credentials, or redirects into the request.
NETWORK_PREFLIGHT_URLS = (
    "https://github.com/jdx/mise/releases/download/v2026.9.14/mise-v2026.9.14-linux-arm64",
)
NETWORK_PREFLIGHT_TIMEOUT = 10

_MODULE_DIR = Path(__file__).resolve().parent
_MODULE_SEARCH_DIRS: list[Path] = []
_SIBLING_MODULES = frozenset({"disk_install", "desktop_payload", "installed_target"})

_SENSITIVE_NAME = re.compile(
    r"(?:pass(?:word|phrase)?|secret|authorized.?key|private.?key|credential|token)",
    re.IGNORECASE,
)


class InstallerError(RuntimeError):
    """An expected, user-actionable installer failure."""


def _now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="seconds")


def _ensure_directories() -> None:
    def secure_directory(path: Path) -> None:
        if not path.is_absolute():
            raise InstallerError("installer state paths must be absolute")
        current = Path(path.anchor)
        parts = path.parts[1:]
        for index, part in enumerate(parts):
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                try:
                    current.mkdir(mode=0o700)
                except OSError as exc:
                    raise InstallerError("installer state directory could not be created") from exc
                info = current.lstat()
            except OSError as exc:
                raise InstallerError("installer state directory could not be inspected") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise InstallerError("installer state path contains a redirected component")
            # In production these paths are fixed below /var and /run and the
            # service runs as root.  The non-root branch keeps unprivileged
            # contract tests usable below their private temporary directory;
            # the final job directories are still required to be private.
            if os.geteuid() == 0 and info.st_uid != 0:
                raise InstallerError("installer state directory is not root-owned")
            if (index == len(parts) - 1 or os.geteuid() == 0) and info.st_mode & 0o022:
                raise InstallerError("installer state directory is writable by another account")
        try:
            path.chmod(0o700)
        except OSError as exc:
            raise InstallerError("installer state directory permissions could not be fixed") from exc

    secure_directory(STATE_ROOT)
    secure_directory(RUNTIME_ROOT)
    secure_directory(STATE_ROOT / "jobs")


def _state_path() -> Path:
    return STATE_ROOT / STATE_FILE_NAME


def _request_path() -> Path:
    return RUNTIME_ROOT / REQUEST_FILE_NAME


def _log_path(job_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", job_id):
        raise InstallerError("invalid job identifier")
    return STATE_ROOT / "jobs" / f"{job_id}.log"


def _atomic_json(path: Path, value: Mapping[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


def _read_json(path: Path, *, missing_ok: bool = False) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise InstallerError("installer state is missing")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError("installer state is unreadable") from exc
    if not isinstance(value, dict):
        raise InstallerError("installer state has an invalid shape")
    return value


def _default_state() -> dict[str, Any]:
    return {"version": 1, "status": "idle", "phase": "idle", "updated_at": _now()}


def _load_state() -> dict[str, Any]:
    value = _read_json(_state_path(), missing_ok=True)
    return _default_state() if value is None else value


def _save_state(state: Mapping[str, Any]) -> None:
    value = dict(state)
    value["version"] = 1
    value["updated_at"] = _now()
    _atomic_json(_state_path(), value)


@contextlib.contextmanager
def _file_lock(path: Path, *, nonblocking: bool = False) -> Iterator[bool]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError:
            yield False
            return
        acquired = True
        yield True
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        else:
            os.close(fd)


def _worker_is_running() -> bool:
    with _file_lock(RUNTIME_ROOT / WORKER_LOCK_NAME, nonblocking=True) as acquired:
        return not acquired


def _service_is_active() -> bool:
    """Treat systemd's activating state as live during the submit race."""

    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "--quiet", SERVICE_NAME],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return result.returncode == 0


def _job_log(job_id: str, message: str, *, secrets_to_hide: Iterable[str] = ()) -> None:
    path = _log_path(job_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    safe = _redact(str(message), iter(secrets_to_hide))
    line = f"{_now()} {safe}\n".encode("utf-8", "replace")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def _redact(text: str, secrets_to_hide: Iterator[str] | None = None) -> str:
    result = text
    values = list(secrets_to_hide or ())
    # Longest first prevents a short password from exposing a prefix of a
    # longer value in an error message.
    for value in sorted((value for value in values if value), key=len, reverse=True):
        result = result.replace(value, "[REDACTED]")
    return result


def _settings_secret_values(settings: Mapping[str, Any]) -> Iterator[str]:
    def visit(value: Any, name: str = "") -> Iterator[str]:
        if isinstance(value, Mapping):
            for key, child in value.items():
                yield from visit(child, str(key))
        elif isinstance(value, list):
            for child in value:
                yield from visit(child, name)
        elif _SENSITIVE_NAME.search(name) and isinstance(value, str):
            yield value

    yield from visit(settings)


def _safe_settings_summary(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Return the deliberately small settings view shown in a terminal."""

    return {
        "username": settings.get("username"),
        "hostname": settings.get("hostname"),
        "timezone": settings.get("timezone"),
        "locale": settings.get("locale"),
        "keymap": settings.get("keymap"),
        "wifi_configured": settings.get("wifi") is not None,
        "ssh_enabled": bool(settings.get("ssh_enabled")),
        "ssh_key_configured": bool(settings.get("ssh_authorized_key")),
        "rdp_mode": settings.get("rdp_mode"),
        "rdp_password_configured": bool(settings.get("rdp_password")),
        "encryption": settings.get("encryption"),
        "recovery_passphrase_configured": bool(settings.get("recovery_passphrase")),
    }


def _safe_identity(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None if value is None else str(value)
    # Disk identity is intentionally returned as data from disk_install.  Do
    # not allow a future implementation to accidentally expose arbitrary
    # nested objects in the terminal protocol.
    result: dict[str, Any] = {}
    for key, child in value.items():
        if _SENSITIVE_NAME.search(str(key)):
            continue
        if isinstance(child, Mapping):
            result[str(key)] = _safe_identity(child)
        elif isinstance(child, (str, int, float, bool)) or child is None:
            result[str(key)] = child
        elif isinstance(child, list):
            result[str(key)] = [_safe_identity(item) if isinstance(item, Mapping) else str(item) for item in child]
    return result


def _safe_summary(value: Any, name: str = "") -> Any:
    if _SENSITIVE_NAME.search(name):
        return "[omitted]"
    if isinstance(value, Mapping):
        return {str(key): _safe_summary(child, str(key)) for key, child in value.items() if not _SENSITIVE_NAME.search(str(key))}
    if isinstance(value, list):
        return [_safe_summary(child, name) for child in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _safe_state(state: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "version",
        "status",
        "phase",
        "job_id",
        "started_at",
        "finished_at",
        "updated_at",
        "error",
        "target_path",
        "target_identity",
        "key_path",
        "key_identity",
        "payload",
        "summary",
        "message",
        "log_file",
        "previous_job_id",
    }
    result: dict[str, Any] = {}
    for key, value in state.items():
        if key not in allowed or _SENSITIVE_NAME.search(key):
            continue
        if key in {"target_identity", "key_identity"}:
            result[key] = _safe_identity(value)
        elif key in {"payload", "summary"}:
            result[key] = _safe_summary(value)
        elif key == "message":
            # Worker progress is redacted before it reaches durable state;
            # keep the value explicit in the public state contract so a
            # reconnecting terminal can show what the worker is doing.
            result[key] = str(value)
        else:
            result[key] = _redact(str(value)) if key == "error" else value
    return result


def _module(name: str) -> Any:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    if name not in _SIBLING_MODULES:
        raise InstallerError(f"installer component unavailable: {name}")
    candidates = [*(Path(directory) for directory in _MODULE_SEARCH_DIRS), _MODULE_DIR]
    for directory in candidates:
        path = directory / f"{name}.py"
        if not path.is_file() or path.is_symlink():
            continue
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        # Register before execution so a sibling can safely refer back to its
        # own module name, matching normal import semantics.
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise
        return module
    raise InstallerError(f"installer component unavailable: {name}")


def _validate_settings(settings: Any) -> dict[str, Any]:
    if not isinstance(settings, Mapping):
        raise InstallerError("settings are not an object")
    expected = {
        "username",
        "hostname",
        "password",
        "timezone",
        "locale",
        "keymap",
        "wifi",
        "ssh_enabled",
        "ssh_authorized_key",
        "rdp_mode",
        "rdp_password",
        "encryption",
        "recovery_passphrase",
    }
    if set(settings) != expected:
        raise InstallerError("settings do not match the installer contract")
    for name in ("username", "hostname", "timezone", "locale", "keymap"):
        if not isinstance(settings[name], str) or not settings[name].strip():
            raise InstallerError("a required text setting is invalid")
    if not isinstance(settings["password"], str) or not settings["password"]:
        raise InstallerError("the target password is required")
    if not isinstance(settings["ssh_enabled"], bool):
        raise InstallerError("SSH selection is invalid")
    if settings["ssh_authorized_key"] is not None and not isinstance(settings["ssh_authorized_key"], str):
        raise InstallerError("the SSH key is invalid")
    if settings["rdp_mode"] not in {"disabled", "loopback", "lan"}:
        raise InstallerError("RDP selection is invalid")
    if settings["rdp_password"] is not None and not isinstance(settings["rdp_password"], str):
        raise InstallerError("the RDP password is invalid")
    if settings["wifi"] is not None:
        wifi = settings["wifi"]
        if not isinstance(wifi, Mapping) or set(wifi) != {"country", "ssid", "password"}:
            raise InstallerError("Wi-Fi settings are invalid")
        if any(not isinstance(wifi[name], str) for name in ("country", "ssid", "password")):
            raise InstallerError("Wi-Fi settings are invalid")
    if settings["encryption"] not in {"plain", "passphrase", "key"}:
        raise InstallerError("encryption selection is invalid")
    if settings["encryption"] == "plain":
        if settings["recovery_passphrase"] not in (None, ""):
            raise InstallerError("plain installation cannot have an unlock passphrase")
    elif not isinstance(settings["recovery_passphrase"], str) or not settings["recovery_passphrase"]:
        raise InstallerError("encrypted installation needs a recovery passphrase")
    validator = getattr(_module("installed_target"), "validate_settings", None)
    if validator is None:
        raise InstallerError("target settings validator is unavailable")
    try:
        validated = validator(dict(settings))
    except InstallerError:
        raise
    except Exception as exc:
        raise InstallerError("target settings were rejected") from exc
    if not isinstance(validated, Mapping):
        raise InstallerError("target settings validator returned an invalid result")
    return dict(validated)


def _target_and_key(request: Mapping[str, Any]) -> tuple[str, str | None]:
    target = request.get("target")
    key = request.get("key")
    if not isinstance(target, str) or not target.startswith("/dev/"):
        raise InstallerError("target disk selection is invalid")
    if key is not None and (not isinstance(key, str) or not key.startswith("/dev/")):
        raise InstallerError("key disk selection is invalid")
    return target, key


def _select_key_disk(disk: Any, path: str) -> Mapping[str, Any]:
    selector = getattr(disk, "select_key_disk", None)
    try:
        identity = selector(path) if selector is not None else disk.select_disk(path)
    except Exception as exc:
        raise InstallerError("the selected key disk is not eligible") from exc
    if not isinstance(identity, Mapping):
        raise InstallerError("key disk selection returned an invalid identity")
    # A disposable key stick is intentionally too small for the target-disk
    # minimum.  The storage module therefore exposes key_eligible separately.
    # Older test doubles omit the field and retain the ordinary selector.
    if "key_eligible" in identity and identity.get("key_eligible") is not True:
        raise InstallerError("the selected key disk is not a blank eligible USB")
    return identity


def _plan(request: Mapping[str, Any]) -> dict[str, Any]:
    settings = _validate_settings(request.get("settings"))
    target, key = _target_and_key(request)
    if settings["encryption"] == "key" and key is None:
        raise InstallerError("key encryption needs a separate key disk")
    if settings["encryption"] != "key" and key is not None:
        raise InstallerError("a key disk is only valid for key encryption")
    disk = _module("disk_install")
    try:
        target_identity = disk.select_disk(target)
        key_identity = _select_key_disk(disk, key) if key else None
        valid = disk.validate_pair(target_identity, key_identity)
        if valid is False:
            raise InstallerError("the selected disk pair was rejected")
        target_token = str(disk.confirm_token(target_identity))
        key_token = str(disk.confirm_token(key_identity)) if key_identity is not None else None
    except InstallerError:
        raise
    except Exception as exc:
        raise InstallerError("the selected disk pair could not be validated") from exc
    payload = _module("desktop_payload")
    try:
        metadata = payload.payload_metadata()
    except Exception as exc:
        raise InstallerError("the desktop payload could not be inspected") from exc
    required_target_bytes = _required_target_bytes(metadata, settings["encryption"] != "plain")
    target_size = _identity_size(target_identity)
    if target_size < required_target_bytes:
        raise InstallerError("the selected target is too small for the verified desktop payload")
    return {
        "settings": settings,
        "settings_summary": _safe_settings_summary(settings),
        "target_path": target,
        "target_identity": _safe_identity(target_identity),
        "key_path": key,
        "key_identity": _safe_identity(key_identity) if key_identity is not None else None,
        "target_token": target_token,
        "key_token": key_token,
        "payload": _safe_summary(metadata),
        "required_target_bytes": required_target_bytes,
        # The raw identities are retained only for the root worker request;
        # all terminal responses use the recursively filtered copies above.
        "target_identity_raw": target_identity,
        "key_identity_raw": key_identity,
    }


def _identity_size(identity: Mapping[str, Any]) -> int:
    value = identity.get("size")
    if value is None and isinstance(identity.get("identity"), Mapping):
        value = identity["identity"].get("size")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise InstallerError("selected target size is unavailable")


def _required_target_bytes(metadata: Mapping[str, Any], encrypted: bool) -> int:
    try:
        payload_bytes = int(metadata["required_target_bytes"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallerError("desktop payload size metadata is invalid") from exc
    # required_target_bytes describes the root contents plus the payload's
    # documented free-space allowance.  The storage module then adds the Pi
    # boot partition; reserve a small extra margin for LUKS metadata.
    boot_bytes = int(getattr(_module("disk_install"), "BOOT_SIZE_MIB", 1024)) * MIB
    return payload_bytes + boot_bytes + (LUKS_OVERHEAD_BYTES if encrypted else 0)


def _network_preflight() -> None:
    for url in NETWORK_PREFLIGHT_URLS:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "omarchy-pi-installer/1"},
            method="HEAD",
        )
        try:
            with urllib.request.urlopen(request, timeout=NETWORK_PREFLIGHT_TIMEOUT) as response:
                if not (200 <= int(response.status) < 400):
                    raise InstallerError("required target setup network access is unavailable")
        except InstallerError:
            raise
        except (OSError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            raise InstallerError("required target setup network access is unavailable") from exc


def _confirmations_match(request: Mapping[str, Any], plan: Mapping[str, Any]) -> None:
    if request.get("target_confirmation") != plan.get("target_token"):
        raise InstallerError("target confirmation did not match the displayed identity")
    if plan.get("key_token") is not None and request.get("key_confirmation") != plan.get("key_token"):
        raise InstallerError("key-disk confirmation did not match the displayed identity")
    if request.get("consent_internet") is not True:
        raise InstallerError("internet consent is required before submission")


def _mark_interrupted(state: dict[str, Any], reason: str) -> dict[str, Any]:
    state["status"] = "interrupted"
    state["phase"] = "interrupted"
    state["error"] = reason
    state["finished_at"] = _now()
    _save_state(state)
    job_id = state.get("job_id")
    if isinstance(job_id, str):
        try:
            _job_log(job_id, f"interrupted: {reason}")
        finally:
            _remove_request(job_id)
    return state


def _mark_stale_if_needed(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("status") in ACTIVE_STATES and not _worker_is_running() and not _service_is_active():
        return _mark_interrupted(state, "the previous installer worker stopped before completion")
    return state


def _new_job_id() -> str:
    return f"{uuid.uuid4().hex[:16]}"


def _write_request(request: Mapping[str, Any]) -> None:
    _atomic_json(_request_path(), dict(request), mode=0o600)


def _remove_request(job_id: str | None = None) -> None:
    path = _request_path()
    if job_id is not None:
        try:
            request = _read_json(path, missing_ok=True)
        except InstallerError:
            # A malformed private request is unusable and must not leave
            # secrets behind in the runtime directory.
            path.unlink(missing_ok=True)
            return
        if request is not None and request.get("job_id") != job_id:
            return
    path.unlink(missing_ok=True)


def _start_service() -> None:
    try:
        subprocess.run(
            ["/usr/bin/systemctl", "start", "--no-block", SERVICE_NAME],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise InstallerError("the installer worker could not be started") from exc


def _submit(request: Mapping[str, Any], *, restart: bool) -> dict[str, Any]:
    state = _mark_stale_if_needed(_load_state())
    if _worker_is_running():
        raise InstallerError("the previous installer worker is still finishing")
    if state.get("status") in ACTIVE_STATES:
        raise InstallerError("an installer job is already running")
    if restart:
        if state.get("status") not in TERMINAL_STATES:
            raise InstallerError("there is no completed or interrupted job to restart")
        if request.get("restart_confirmation") != RESTART_CONFIRMATION:
            raise InstallerError("restart requires the exact confirmation phrase")
    elif state.get("status") not in {"idle"}:
        raise InstallerError("restart is required before replacing the existing job")
    plan = _plan(request)
    _confirmations_match(request, plan)
    if restart:
        payload_module = _module("desktop_payload")
        reset = getattr(payload_module, "reset_incomplete_payload", None)
        if reset is None:
            raise InstallerError("payload restart cleanup is unavailable")
        try:
            # This is intentionally reachable only through the exact restart
            # confirmation branch above.  The helper removes only its own
            # positively marked incomplete extraction.
            reset()
        except Exception as exc:
            raise InstallerError("the incomplete payload could not be safely reset") from exc
    job_id = _new_job_id()
    previous_job_id = state.get("job_id") if restart else None
    private_request = {
        "version": 1,
        "job_id": job_id,
        "settings": plan["settings"],
        "target": plan["target_path"],
        "key": plan["key_path"],
        "target_identity": plan["target_identity_raw"],
        "key_identity": plan["key_identity_raw"],
        "target_token": plan["target_token"],
        "key_token": plan["key_token"],
        "required_target_bytes": plan["required_target_bytes"],
        "consent_internet": True,
    }
    # The request is written before the durable queued state.  A power loss
    # between these writes is still handled as an interrupted job, and the
    # request disappears with /run; it can never be auto-resumed.
    try:
        _write_request(private_request)
    except Exception:
        _remove_request(job_id)
        raise
    state = {
        "version": 1,
        "status": "queued",
        "phase": "queued",
        "job_id": job_id,
        "started_at": _now(),
        "target_path": plan["target_path"],
        "target_identity": plan["target_identity"],
        "key_path": plan["key_path"],
        "key_identity": plan["key_identity"],
        "payload": plan["payload"],
        "log_file": str(_log_path(job_id)),
    }
    if previous_job_id:
        state["previous_job_id"] = previous_job_id
    try:
        _save_state(state)
        _job_log(job_id, "queued after explicit disk and settings confirmation")
    except Exception:
        _remove_request(job_id)
        raise InstallerError("the installer job could not be queued")
    try:
        _start_service()
    except InstallerError as exc:
        _remove_request(job_id)
        state["status"] = "failed"
        state["phase"] = "worker-start"
        state["error"] = str(exc)
        state["finished_at"] = _now()
        _save_state(state)
        _job_log(job_id, "worker start failed")
        raise
    return {"job_id": job_id, "state": _safe_state(state)}


def _worker_progress(
    job_id: str,
    phase: str,
    message: str,
    *,
    secrets_to_hide: Iterable[str] = (),
) -> None:
    state = _load_state()
    if state.get("job_id") != job_id:
        raise InstallerError("installer state changed while the worker was running")
    safe_phase = _redact(phase, iter(secrets_to_hide))
    safe_message = _redact(str(message), iter(secrets_to_hide))
    state["phase"] = safe_phase
    state["message"] = safe_message
    _save_state(state)
    _job_log(job_id, f"{phase}: {message}", secrets_to_hide=secrets_to_hide)


def _validate_worker_disk(request: Mapping[str, Any]) -> None:
    disk = _module("disk_install")
    target = request["target"]
    key = request.get("key")
    target_identity = request.get("target_identity")
    key_identity = request.get("key_identity")
    if not isinstance(target_identity, Mapping) or (key is not None and not isinstance(key_identity, Mapping)):
        raise InstallerError("the confirmed disk identities are unavailable")
    try:
        current_target, current_key = disk.validate_pair(target_identity, key_identity)
        if current_target.get("path") != target or str(disk.confirm_token(current_target)) != request.get("target_token"):
            raise InstallerError("the target disk identity changed after confirmation")
        if key is not None and current_key is None:
            raise InstallerError("the confirmed key disk identity disappeared")
        if current_key is not None and (
            current_key.get("path") != key or str(disk.confirm_token(current_key)) != request.get("key_token")
        ):
            raise InstallerError("the key disk identity changed after confirmation")
        if _identity_size(current_target) < int(request.get("required_target_bytes", 0)):
            raise InstallerError("the selected target is too small for the verified desktop payload")
    except InstallerError:
        raise
    except Exception as exc:
        raise InstallerError("the selected disks are no longer eligible") from exc


def _run_worker() -> int:
    _ensure_directories()
    with _file_lock(RUNTIME_ROOT / WORKER_LOCK_NAME, nonblocking=True) as acquired:
        if not acquired:
            return 75
        state = _load_state()
        if state.get("status") != "queued":
            return 0
        job_id = state.get("job_id")
        if not isinstance(job_id, str):
            return 1
        private_request = _read_json(_request_path(), missing_ok=True)
        if private_request is None or private_request.get("job_id") != job_id:
            _mark_interrupted(state, "the queued request is unavailable; explicit restart is required")
            return 1
        settings = private_request.get("settings")
        secret_values = tuple(_settings_secret_values(settings)) if isinstance(settings, Mapping) else ()
        try:
            settings = _validate_settings(settings)
            state["status"] = "running"
            state["phase"] = "payload-validation"
            state["started_at"] = state.get("started_at", _now())
            _save_state(state)
            _job_log(job_id, "worker started")
            payload = _module("desktop_payload")
            _worker_progress(
                job_id,
                "payload-validation",
                "verifying the desktop payload",
                secrets_to_hide=secret_values,
            )
            prepared_payload = payload.prepare_payload()
            if not isinstance(prepared_payload, (str, Path)):
                raise InstallerError("desktop payload preparation returned an invalid path")
            prepared_payload = Path(prepared_payload)
            if not prepared_payload.is_dir():
                raise InstallerError("desktop payload preparation did not produce a directory")
            target_module = _module("installed_target")
            validate_options = getattr(target_module, "validate_target_options", None)
            if validate_options is not None:
                try:
                    options_result = validate_options(prepared_payload / "rootfs", settings)
                except Exception as exc:
                    raise InstallerError("target regional settings are unavailable in the verified payload") from exc
                if options_result is False:
                    raise InstallerError("target regional settings are unavailable in the verified payload")
            _worker_progress(
                job_id,
                "network-preflight",
                "checking fixed target setup network access",
                secrets_to_hide=secret_values,
            )
            _network_preflight()
            metadata = payload.payload_metadata()
            private_request["required_target_bytes"] = _required_target_bytes(
                metadata, settings["encryption"] != "plain"
            )
            _validate_worker_disk(private_request)
            _worker_progress(
                job_id,
                "target-preparation",
                "preparing the selected target",
                secrets_to_hide=secret_values,
            )
            disk = _module("disk_install")
            passphrase = settings.get("recovery_passphrase")
            storage_context = disk.prepare_target(
                private_request["target_identity"],
                settings["encryption"],
                passphrase,
                private_request.get("key_identity"),
            )
            with storage_context as storage:
                copy_payload = getattr(payload, "copy_payload", None)
                if copy_payload is None:
                    raise InstallerError("desktop payload copier is unavailable")
                _worker_progress(
                    job_id,
                    "payload-copy",
                    "copying the verified desktop payload",
                    secrets_to_hide=secret_values,
                )
                copy_payload(prepared_payload, Path(storage["root"]), Path(storage["boot"]))
                _worker_progress(
                    job_id,
                    "desktop-provisioning",
                    "configuring the target",
                    secrets_to_hide=secret_values,
                )
                result = target_module.provision_target(
                    Path(storage["root"]),
                    prepared_payload,
                    settings,
                    storage,
                    lambda message: _worker_progress(
                        job_id,
                        "desktop-provisioning",
                        str(message),
                        secrets_to_hide=secret_values,
                    ),
                )
                state = _load_state()
                state["phase"] = "finalizing"
                _save_state(state)
            state = _load_state()
            state["status"] = "complete"
            state["phase"] = "complete"
            state["summary"] = _safe_summary(result)
            state["finished_at"] = _now()
            _save_state(state)
            _job_log(job_id, "installation complete")
            return 0
        except KeyboardInterrupt:
            # Deliberately leave ``running`` durable.  The next observer sees
            # that the lock has gone away and converts it to interrupted.
            raise
        except Exception as exc:
            message = _redact(f"{type(exc).__name__}: {exc}", secret_values)
            state = _load_state()
            state["status"] = "failed"
            state["phase"] = state.get("phase", "worker")
            state["error"] = message
            state["finished_at"] = _now()
            _save_state(state)
            _job_log(job_id, f"installation failed: {message}", secrets_to_hide=secret_values)
            return 1
        finally:
            _remove_request(job_id)


def _handle_request(request: Mapping[str, Any]) -> dict[str, Any]:
    action = request.get("action")
    if action == "status":
        with _file_lock(RUNTIME_ROOT / CONTROL_LOCK_NAME):
            state = _mark_stale_if_needed(_load_state())
            return {"state": _safe_state(state)}
    if action == "discover":
        disks = _module("disk_install").discover_disks()
        if not isinstance(disks, list):
            raise InstallerError("disk discovery returned an invalid result")
        return {"disks": [_safe_identity(item) for item in disks]}
    if action == "plan":
        plan = _plan(request)
        return {
            "settings": plan["settings_summary"],
            "payload": plan["payload"],
            "target": {"path": plan["target_path"], "identity": plan["target_identity"], "token": plan["target_token"]},
            "key": (
                {"path": plan["key_path"], "identity": plan["key_identity"], "token": plan["key_token"]}
                if plan["key_path"]
                else None
            ),
        }
    if action in {"submit", "restart"}:
        with _file_lock(RUNTIME_ROOT / CONTROL_LOCK_NAME):
            return _submit(request, restart=action == "restart")
    raise InstallerError("unknown installer control action")


def _read_control_request(stream: Any) -> dict[str, Any]:
    raw = stream.buffer.read(MAX_INPUT_BYTES + 1) if hasattr(stream, "buffer") else stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise InstallerError("control request is too large")
    try:
        value = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError("control request is not valid JSON") from exc
    if not isinstance(value, dict):
        raise InstallerError("control request is not an object")
    return value


def control_main() -> int:
    if os.geteuid() != 0:
        print(json.dumps({"ok": False, "error": "installer control requires root"}))
        return 77
    _ensure_directories()
    try:
        request = _read_control_request(sys.stdin)
        result = _handle_request(request)
    except InstallerError as exc:
        result = {"ok": False, "error": _redact(str(exc))}
    except Exception:
        # Never print subprocess output or an arbitrary traceback: either can
        # contain a submitted secret.
        result = {"ok": False, "error": "installer control failed"}
    else:
        result = {"ok": True, **result}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


def _ask(output: Callable[[str], None], prompt: str, input_fn: Callable[[str], str]) -> str:
    return input_fn(prompt).strip()


def _ask_yes(output: Callable[[str], None], prompt: str, input_fn: Callable[[str], str]) -> bool:
    return _ask(output, f"{prompt} [yes/no] ", input_fn).lower() in {"yes", "y"}


def _client_call(request: Mapping[str, Any], runner: Callable[..., Any] | None = None) -> dict[str, Any]:
    encoded = json.dumps(dict(request), separators=(",", ":")) + "\n"
    invoke = runner or subprocess.run
    try:
        completed = invoke(
            ["/usr/bin/sudo", "-n", CONTROL_PATH],
            input=encoded,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise InstallerError("installer control is unavailable") from exc
    if completed.returncode != 0 and not completed.stdout:
        raise InstallerError("installer control was not authorized")
    try:
        response = json.loads(completed.stdout)
    except (TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError("installer control returned invalid data") from exc
    if not isinstance(response, dict) or not response.get("ok"):
        raise InstallerError(str(response.get("error", "installer control rejected the request")))
    return response


def _frontend_collect(input_fn: Callable[[str], str], secret_fn: Callable[[str], str]) -> tuple[dict[str, Any], str | None]:
    username = input_fn("Target username: ").strip()
    hostname = input_fn("Target hostname: ").strip()
    password = secret_fn("Target account password: ")
    timezone = input_fn("Timezone [Europe/London]: ").strip() or "Europe/London"
    locale = input_fn("Locale [en_GB.UTF-8]: ").strip() or "en_GB.UTF-8"
    keymap = input_fn("Keyboard layout [us]: ").strip() or "us"
    wifi: dict[str, str] | None = None
    if _ask_yes(print, "Configure Wi-Fi?", input_fn):
        wifi = {
            "country": input_fn("Wi-Fi country: ").strip(),
            "ssid": input_fn("Wi-Fi SSID: ").strip(),
            "password": secret_fn("Wi-Fi password: "),
        }
    ssh_enabled = _ask_yes(print, "Enable SSH on the installed target?", input_fn)
    ssh_key = input_fn("Target SSH authorized key (blank to omit): ").strip() or None if ssh_enabled else None
    rdp_mode = input_fn("RDP mode [disabled/loopback/lan]: ").strip().lower() or "disabled"
    rdp_password = secret_fn("Target RDP password: ") if rdp_mode != "disabled" else None
    encryption = input_fn("Storage encryption [plain/passphrase/key]: ").strip().lower() or "plain"
    recovery = secret_fn("Recovery passphrase: ") if encryption != "plain" else None
    settings = {
        "username": username,
        "hostname": hostname,
        "password": password,
        "timezone": timezone,
        "locale": locale,
        "keymap": keymap,
        "wifi": wifi,
        "ssh_enabled": ssh_enabled,
        "ssh_authorized_key": ssh_key,
        "rdp_mode": rdp_mode,
        "rdp_password": rdp_password,
        "encryption": encryption,
        "recovery_passphrase": recovery,
    }
    return settings, None


def _print_finished_state(state: Mapping[str, Any], output: Callable[[str], None]) -> None:
    status = state.get("status")
    if status == "complete":
        output("Installation complete. Reconnect details:")
        if state.get("summary") is not None:
            output(json.dumps(state["summary"], indent=2, sort_keys=True))
        output("Shut down manually, remove the installer USB, retain any unlock-key USB, and boot the target.")
        return
    if status in {"failed", "interrupted"}:
        output(f"The previous installer job is {status}.")
        if state.get("error"):
            output(f"Reason: {state['error']}")
        if state.get("log_file"):
            output(f"Diagnostic log: {state['log_file']}")


def _watch_job(
    state: Mapping[str, Any],
    *,
    call: Callable[[Mapping[str, Any]], dict[str, Any]],
    output: Callable[[str], None],
    sleep_fn: Callable[[float], None],
) -> int:
    current = dict(state)
    last_message = current.get("message")
    initial = f"Installer job {current.get('job_id')} is {current.get('status')} ({current.get('phase')})."
    if isinstance(last_message, str) and last_message:
        initial += f" {last_message}"
    output(initial)
    output("You may disconnect; reconnect later to observe the same job.")
    while current.get("status") in ACTIVE_STATES:
        sleep_fn(POLL_SECONDS)
        current = call({"action": "status"})["state"]
        message = current.get("message")
        progress = f"{current.get('status')}: {current.get('phase')}"
        if isinstance(message, str) and message and message != last_message:
            progress += f" — {message}"
        last_message = message
        output(progress)
    _print_finished_state(current, output)
    return 0 if current.get("status") == "complete" else 1


def _hold_after_watch(input_fn: Callable[[str], str]) -> None:
    """Keep the terminal open long enough for completion guidance to be read."""

    try:
        input_fn("Press Enter to close the installer view...")
    except (EOFError, KeyboardInterrupt):
        # Closing a disconnected terminal must not affect the worker or job;
        # it only skips the optional pause before returning to the shell.
        return


def frontend_main(
    *,
    input_fn: Callable[[str], str] = input,
    secret_fn: Callable[[str], str] | None = None,
    output: Callable[[str], None] = print,
    call: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """Run the same line-oriented flow on a local TTY, SSH, or RDP terminal."""

    if secret_fn is None:
        import getpass

        secret_fn = getpass.getpass
    if call is None:
        call = _client_call
    try:
        response = call({"action": "status"})
        state = response["state"]
        if state.get("status") in {"queued", "running"}:
            result = _watch_job(state, call=call, output=output, sleep_fn=sleep_fn)
            _hold_after_watch(input_fn)
            return result
        restart = state.get("status") in {"interrupted", "failed", "complete"}
        if restart:
            _print_finished_state(state, output)
            if state.get("status") == "complete":
                prompt = "Type the exact restart phrase to begin another install, or press Enter to leave the result: "
            else:
                prompt = f"Type {RESTART_CONFIRMATION!r} to restart from the beginning, or press Enter to leave: "
            if input_fn(prompt).strip() != RESTART_CONFIRMATION:
                output("Restart cancelled; the recorded result remains available.")
                return 0 if state.get("status") == "complete" else 1
        disks = call({"action": "discover"}).get("disks", [])
        for disk in disks:
            output(json.dumps(disk, sort_keys=True))
        target = input_fn("Target disk path exactly as listed: ").strip()
        settings, _ = _frontend_collect(input_fn, secret_fn)
        key = input_fn("Disposable key disk path (required only for key encryption): ").strip() or None
        plan_request = {"action": "plan", "settings": settings, "target": target, "key": key}
        plan = call(plan_request)
        output("Review this secret-free plan:")
        output(json.dumps({key: value for key, value in plan.items() if key != "ok"}, indent=2, sort_keys=True))
        target_confirmation = input_fn("Type the displayed target token exactly: ").strip()
        key_confirmation = input_fn("Type the displayed key token exactly: ").strip() if plan.get("key") else None
        if input_fn("Type YES to consent to target setup needing internet access: ").strip() != "YES":
            output("Submission cancelled.")
            return 1
        submit = {
            "action": "restart" if restart else "submit",
            "settings": settings,
            "target": target,
            "key": key,
            "target_confirmation": target_confirmation,
            "key_confirmation": key_confirmation,
            "consent_internet": True,
        }
        result = call({**submit, "restart_confirmation": RESTART_CONFIRMATION} if restart else submit)
        watch_result = _watch_job(result["state"], call=call, output=output, sleep_fn=sleep_fn)
        _hold_after_watch(input_fn)
        return watch_result
    except (EOFError, KeyboardInterrupt):
        output("Installer input cancelled.")
        return 1
    except InstallerError as exc:
        output(_redact(str(exc)))
        return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in {"--control", "--worker", "--frontend"}:
        print("installer_job.py is an internal fixed entrypoint", file=sys.stderr)
        return 64
    if argv[0] == "--control":
        return control_main()
    if argv[0] == "--worker":
        if os.geteuid() != 0:
            return 77
        return _run_worker()
    return frontend_main()


if __name__ == "__main__":
    raise SystemExit(main())
