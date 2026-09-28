#!/usr/bin/python3
"""Bounded, offline recovery operations for an installed Pi target.

This module is deliberately separate from :mod:`disk_install`.  The latter
owns a new installation and is allowed to partition and format a blank disk;
this module only identifies an existing installation, opens an existing LUKS
volume read-only, and performs a narrowly scoped boot repair after an explicit
confirmation.  It never adds or removes a LUKS key, formats storage, changes
the EEPROM, rewrites the kernel command line, or provisions an account.

The public functions accept an injected command runner.  That keeps the
filesystem and command boundaries testable with file-backed fixtures.  The
installed CLI sends requests to the existing argument-free privileged
``installer-control`` entrypoint; secrets are supplied on its stdin JSON
request and are never put in an argv vector.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid


class RecoveryError(RuntimeError):
    """Raised when a recovery request crosses a safety boundary."""


Runner = Callable[..., Any]

RECOVERY_MAPPER = "omarchy-pi-recovery-cryptroot"
REPAIR_CONFIRMATION = "REPAIR BOOT ONLY"
REINSTALL_CONFIRMATION = "REINSTALL VIA INSTALLER"
CONTROLLER_PATH = "/usr/local/libexec/omarchy-pi/installer-control"
MOUNT_ROOT = Path("/run/omarchy-pi/recovery")
SYSFS_ROOT = Path("/sys/class/block")

_STABLE_VALUE = re.compile(r"[A-Za-z0-9._:+/-]{1,256}\Z")
_MAPPER_NAME = re.compile(r"omarchy-pi-recovery-[a-z0-9-]{1,48}\Z")
_UUID = re.compile(r"(?:[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}|[0-9A-Fa-f]{8,64})\Z")
_SAFE_KERNEL = re.compile(r"(?:kernel8\.img|kernel_2712\.img|Image)\Z")
_KERNEL_ASSIGNMENT = re.compile(r"^\s*kernel=(\S+)\s*$", re.IGNORECASE)
_FSTYPE_BOOT = frozenset({"vfat", "fat", "fat16", "fat32", "msdos"})
_FSTYPE_LUKS = frozenset({"crypto_luks", "luks", "luks2"})
_FSTYPE_ROOT = frozenset({"ext4"})


def _fail(message: str) -> RecoveryError:
    # Error strings are fixed descriptions.  Do not interpolate credential
    # values or command output into errors returned through the controller.
    return RecoveryError(message)


def _run(
    runner: Runner,
    command: Sequence[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> Any:
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise _fail("invalid command vector")
    try:
        result = runner(
            list(command),
            input=input_text,
            text=True,
            capture_output=True,
            check=check,
        )
    except TypeError:
        # A small number of tests use runners that expose the existing
        # disk_install ``input_text`` spelling.  Keep the production path on
        # the standard subprocess.run signature.
        try:
            result = runner(list(command), input_text=input_text, check=check)
        except Exception as exc:  # pragma: no cover - defensive boundary
            raise _fail(f"command failed to start: {command[0]}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise _fail(f"command failed to start: {command[0]}") from exc
    returncode = getattr(result, "returncode", 0)
    if check and returncode != 0:
        raise _fail(f"command failed: {command[0]}")
    return result


def _stdout(result: Any) -> str:
    value = getattr(result, "stdout", "")
    return value if isinstance(value, str) else str(value or "")


def _absolute(path: str | Path, *, description: str) -> Path:
    value = Path(path)
    if not value.is_absolute():
        raise _fail(f"{description} must be absolute")
    return Path(os.path.normpath(os.fspath(value)))


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _fail("recovery path cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode):
            raise _fail("recovery path contains a symlink")


def _regular_file(path: Path, *, description: str, nonempty: bool = True) -> Path:
    _reject_symlink_components(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise _fail(f"{description} is missing") from None
    except OSError as exc:
        raise _fail(f"{description} cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _fail(f"{description} must be a regular file")
    if nonempty and info.st_size == 0:
        raise _fail(f"{description} is empty")
    return path


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "yes", "true", "on"}


def _normalise_mountpoints(node: Mapping[str, Any]) -> list[str]:
    values = node.get("mountpoints", node.get("mountpoint", []))
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Iterable):
        return []
    return [str(value) for value in values if value]


def _children(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for raw in node.get("children", []) or []:
        if not isinstance(raw, Mapping):
            continue
        result.append(raw)
        result.extend(_children(raw))
    return result


def _path(node: Mapping[str, Any]) -> str | None:
    value = _clean(node.get("path"))
    if value:
        return value
    kname = _clean(node.get("kname"))
    return f"/dev/{kname}" if kname else None


def _device_number(node: Mapping[str, Any]) -> str | None:
    return _clean(node.get("maj:min") or node.get("major_minor") or node.get("MAJ:MIN"))


def _parse_lsblk(result: Any) -> list[dict[str, Any]]:
    try:
        document = json.loads(_stdout(result))
    except (TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("block-device inventory is not valid JSON") from exc
    if not isinstance(document, Mapping):
        raise _fail("block-device inventory is not an object")
    raw_nodes = document.get("blockdevices", [])
    if not isinstance(raw_nodes, list):
        raise _fail("block-device inventory has an invalid shape")
    return [dict(node) for node in raw_nodes if isinstance(node, Mapping)]


def _safe_stable(value: Any) -> str | None:
    text = _clean(value)
    return text if text and _STABLE_VALUE.fullmatch(text) else None


def _safe_uuid(value: Any) -> str | None:
    text = _clean(value)
    if not text:
        return None
    if _UUID.fullmatch(text) or re.fullmatch(r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}", text):
        return text
    return None


def _sysfs_cid(path: str) -> str | None:
    candidate = SYSFS_ROOT / Path(path).name / "device/cid"
    try:
        value = candidate.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    return _safe_stable(value)


def _stable_id(node: Mapping[str, Any], *, udev: Mapping[str, str] | None = None, sysfs_cid: str | None = None) -> str | None:
    for name, prefix in (("serial", "serial"), ("wwn", "wwn"), ("cid", "cid")):
        value = _safe_stable(node.get(name))
        if value:
            return f"{prefix}:{value}"
    props = udev or {}
    for name, prefix in (("ID_SERIAL", "serial"), ("ID_WWN", "wwn"), ("ID_SERIAL_SHORT", "serial"), ("ID_PART_ENTRY_DISK", "disk")):
        value = _safe_stable(props.get(name))
        if value:
            return f"{prefix}:{value}"
    value = _safe_stable(sysfs_cid)
    if value:
        return f"cid:{value}"
    return None


def _read_udev(path: str, runner: Runner) -> dict[str, str]:
    result = _run(runner, ["udevadm", "info", "--query=property", "--name", path], check=False)
    if getattr(result, "returncode", 0) != 0:
        return {}
    properties: dict[str, str] = {}
    for line in _stdout(result).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            properties[key] = value
    return properties


def _findmnt_sources(runner: Runner) -> tuple[set[str], set[str]]:
    """Return mounted sources and device numbers; failure is a hard refusal."""

    result = _run(runner, ["findmnt", "--json", "--output", "SOURCE,MAJ:MIN,TARGET"], check=False)
    if getattr(result, "returncode", 0) != 0:
        raise _fail("mount inventory is unavailable")
    try:
        document = json.loads(_stdout(result))
    except (TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("mount inventory is not valid JSON") from exc
    sources: set[str] = set()
    numbers: set[str] = set()

    def visit(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, Mapping):
                continue
            source = _clean(item.get("source"))
            number = _clean(item.get("maj:min") or item.get("major_minor"))
            if source:
                sources.add(source)
            if number:
                numbers.add(number)
            visit(item.get("children"))

    visit(document.get("filesystems"))
    return sources, numbers


def _swap_sources(runner: Runner) -> set[str]:
    result = _run(runner, ["cat", "/proc/swaps"], check=False)
    if getattr(result, "returncode", 0) != 0:
        raise _fail("swap inventory is unavailable")
    sources: set[str] = set()
    for line in _stdout(result).splitlines()[1:]:
        fields = line.split()
        if fields:
            sources.add(fields[0])
    return sources


def _marker(node: Mapping[str, Any], *, installer: bool) -> bool:
    expected = "OMARCHY-INSTALLER" if installer else "OMARCHYKEY"
    if _clean(node.get("label")) and _clean(node.get("label")).upper() == expected:
        return True
    key = "installer_media" if installer else "key_media"
    return _bool(node.get(key)) or _bool(node.get("installer_image_marker" if installer else "key_marker"))


def _partition_kind(node: Mapping[str, Any]) -> str:
    label = (_clean(node.get("label")) or "").upper()
    fstype = (_clean(node.get("fstype")) or "").lower()
    if label in {"PI-BOOT", "BOOT", "OMARCHY-BOOT"} or fstype in _FSTYPE_BOOT:
        return "boot"
    if label in {"OMARCHY-ROOT", "ROOT"} or fstype in _FSTYPE_LUKS or fstype in _FSTYPE_ROOT:
        return "root"
    return "unknown"


def _device_is_mounted(node: Mapping[str, Any], mounted_sources: set[str], mounted_numbers: set[str]) -> bool:
    for item in [node, *_children(node)]:
        path = _path(item)
        if path and path in mounted_sources:
            return True
        number = _device_number(item)
        if number and number in mounted_numbers:
            return True
        if _normalise_mountpoints(item):
            return True
    return False


@dataclass(frozen=True, slots=True)
class TargetIdentity:
    """Stable, non-secret identity and eligibility facts for one disk."""

    path: str
    stable_id: str | None
    size: int
    transport: str | None
    root_device: str | None
    boot_device: str | None
    root_uuid: str | None
    boot_uuid: str | None
    luks_uuid: str | None
    root_fstype: str | None
    mounted: bool
    installer_media: bool
    key_media: bool
    reasons: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        return not self.reasons

    @property
    def token(self) -> str:
        stable = self.stable_id or "<missing>"
        root_uuid = self.root_uuid or "<missing>"
        boot_uuid = self.boot_uuid or "<missing>"
        return f"RECOVER {self.path} {stable} {root_uuid} {boot_uuid}"

    @property
    def encrypted(self) -> bool:
        return bool(self.luks_uuid)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "stable_id": self.stable_id,
            "size": self.size,
            "transport": self.transport,
            "root_device": self.root_device,
            "boot_device": self.boot_device,
            "root_uuid": self.root_uuid,
            "boot_uuid": self.boot_uuid,
            "luks_uuid": self.luks_uuid,
            "root_fstype": self.root_fstype,
            "mounted": self.mounted,
            "installer_media": self.installer_media,
            "key_media": self.key_media,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "token": self.token,
        }


def _identity_for(
    node: Mapping[str, Any],
    *,
    runner: Runner,
    mounted_sources: set[str],
    mounted_numbers: set[str],
    swaps: set[str],
) -> TargetIdentity:
    path = _path(node)
    if not path:
        raise _fail("block-device inventory contains a disk without a path")
    udev = _read_udev(path, runner)
    stable_id = _stable_id(node, udev=udev, sysfs_cid=_sysfs_cid(path))
    descendants = _children(node)
    partitions = [item for item in descendants if _clean(item.get("type")) == "part"]
    boots = [item for item in partitions if _partition_kind(item) == "boot"]
    roots = [item for item in partitions if _partition_kind(item) == "root"]
    reasons: list[str] = []
    if _clean(node.get("type")) != "disk":
        reasons.append("not a whole physical disk")
    if path.startswith("/dev/loop") or _clean(node.get("type")) in {"loop", "rom", "dm", "crypt", "md", "lvm"}:
        reasons.append("virtual or non-writable device")
    if _bool(node.get("ro")) or _bool(node.get("read_only")):
        reasons.append("read-only device")
    if not stable_id:
        reasons.append("no stable serial, WWN, or SD CID identity")
    if _int(node.get("size")) <= 0:
        reasons.append("unknown device size")
    mounted = _device_is_mounted(node, mounted_sources, mounted_numbers)
    if mounted:
        reasons.append("mounted filesystem or descendant")
    node_path = path
    if node_path in swaps or any(_path(item) in swaps for item in descendants):
        reasons.append("contains active swap")
    installer_media = any(_marker(item, installer=True) for item in [node, *descendants])
    key_media = any(_marker(item, installer=False) for item in [node, *descendants])
    if installer_media:
        reasons.append("installer media")
    if key_media:
        reasons.append("protected existing unlock-key media")
    if len(boots) != 1:
        reasons.append("boot partition is missing or ambiguous")
    if len(roots) != 1:
        reasons.append("root partition is missing or ambiguous")
    boot = boots[0] if len(boots) == 1 else {}
    root = roots[0] if len(roots) == 1 else {}
    boot_device = _path(boot)
    root_device = _path(root)
    boot_uuid = _safe_uuid(boot.get("uuid"))
    root_uuid = _safe_uuid(root.get("uuid"))
    root_fstype = _clean(root.get("fstype"))
    luks_uuid = root_uuid if (root_fstype or "").lower() in _FSTYPE_LUKS else None
    if not boot_uuid:
        reasons.append("boot UUID is unavailable")
    if not root_uuid:
        reasons.append("root UUID is unavailable")
    if luks_uuid is None and (root_fstype or "").lower() not in _FSTYPE_ROOT:
        reasons.append("root filesystem type is unsupported")
    if boot_device is None or root_device is None:
        reasons.append("target partition paths are unavailable")
    if boot_device and root_device and boot_device == root_device:
        reasons.append("boot and root partitions are identical")
    return TargetIdentity(
        path=path,
        stable_id=stable_id,
        size=_int(node.get("size")),
        transport=_clean(node.get("tran")),
        root_device=root_device,
        boot_device=boot_device,
        root_uuid=root_uuid,
        boot_uuid=boot_uuid,
        luks_uuid=luks_uuid,
        root_fstype=root_fstype,
        mounted=mounted,
        installer_media=installer_media,
        key_media=key_media,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def discover_targets(*, runner: Runner = subprocess.run, inventory: Mapping[str, Any] | None = None) -> list[TargetIdentity]:
    """Build a read-only inventory of physical disks and recovery eligibility."""

    if inventory is None:
        result = _run(
            runner,
            [
                "lsblk",
                "--json",
                "--tree",
                "--paths",
                "--bytes",
                "--output",
                "PATH,KNAME,TYPE,SIZE,SERIAL,WWN,TRAN,FSTYPE,LABEL,UUID,PKNAME,RO,MOUNTPOINTS,MAJ:MIN,PTTYPE",
            ],
        )
        nodes = _parse_lsblk(result)
        mounted_sources, mounted_numbers = _findmnt_sources(runner)
        swaps = _swap_sources(runner)
    else:
        raw_nodes = inventory.get("blockdevices") if isinstance(inventory, Mapping) else None
        if not isinstance(raw_nodes, list):
            raise _fail("block-device inventory has an invalid shape")
        nodes = [dict(node) for node in raw_nodes if isinstance(node, Mapping)]
        mounted_sources, mounted_numbers, swaps = set(), set(), set()
    result = [
        _identity_for(
            node,
            runner=runner,
            mounted_sources=mounted_sources,
            mounted_numbers=mounted_numbers,
            swaps=swaps,
        )
        for node in nodes
        if _clean(node.get("type")) == "disk"
    ]
    stable_counts: dict[str, int] = {}
    for identity in result:
        if identity.stable_id:
            stable_counts[identity.stable_id] = stable_counts.get(identity.stable_id, 0) + 1
    return [
        replace(identity, reasons=identity.reasons + ("stable identity is ambiguous",))
        if identity.stable_id and stable_counts[identity.stable_id] > 1
        else identity
        for identity in result
    ]


def _identity_from(value: TargetIdentity | Mapping[str, Any]) -> TargetIdentity:
    if isinstance(value, TargetIdentity):
        return value
    if not isinstance(value, Mapping):
        raise _fail("target identity is invalid")
    required = {"path", "stable_id", "size", "root_device", "boot_device", "root_uuid", "boot_uuid", "luks_uuid", "root_fstype", "mounted", "installer_media", "key_media"}
    if not required.issubset(value):
        raise _fail("target identity is incomplete")
    try:
        return TargetIdentity(
            path=str(value["path"]),
            stable_id=_clean(value["stable_id"]),
            size=int(value["size"]),
            transport=_clean(value.get("transport")),
            root_device=_clean(value["root_device"]),
            boot_device=_clean(value["boot_device"]),
            root_uuid=_clean(value["root_uuid"]),
            boot_uuid=_clean(value["boot_uuid"]),
            luks_uuid=_clean(value["luks_uuid"]),
            root_fstype=_clean(value["root_fstype"]),
            mounted=bool(value["mounted"]),
            installer_media=bool(value["installer_media"]),
            key_media=bool(value["key_media"]),
            reasons=tuple(str(item) for item in value.get("reasons", [])),
        )
    except (TypeError, ValueError) as exc:
        raise _fail("target identity is invalid") from exc


def select_target(
    path: str,
    *,
    confirmation: str | None = None,
    runner: Runner = subprocess.run,
    identity: TargetIdentity | Mapping[str, Any] | None = None,
) -> TargetIdentity:
    """Select and revalidate one explicitly named target disk."""

    if not isinstance(path, str) or not path.startswith("/dev/") or path == "/dev/":
        raise _fail("an explicit target device is required")
    candidates = discover_targets(runner=runner)
    matches = [item for item in candidates if item.path == path]
    if len(matches) != 1:
        raise _fail("target is not a uniquely discovered physical disk")
    selected = matches[0]
    if identity is not None:
        previous = _identity_from(identity)
        if previous.path != selected.path or previous.stable_id != selected.stable_id or previous.size != selected.size:
            raise _fail("target stable identity or size changed")
        if previous.root_uuid != selected.root_uuid or previous.boot_uuid != selected.boot_uuid or previous.luks_uuid != selected.luks_uuid:
            raise _fail("target partition UUID changed")
    if confirmation is not None and confirmation != selected.token:
        raise _fail("target confirmation did not match the current identity")
    if not selected.eligible:
        raise _fail("target is refused: " + "; ".join(selected.reasons))
    return selected


def _validate_mapper(mapper: str) -> str:
    if not isinstance(mapper, str) or not _MAPPER_NAME.fullmatch(mapper):
        raise _fail("recovery mapper name is invalid")
    return mapper


def _validate_key_file(path: str | Path) -> Path:
    candidate = _absolute(path, description="key file")
    _regular_file(candidate, description="key file")
    try:
        info = candidate.stat()
    except OSError as exc:
        raise _fail("key file cannot be inspected") from exc
    if info.st_mode & 0o077:
        raise _fail("key file permissions are too broad")
    if os.geteuid() == 0 and info.st_uid != 0:
        raise _fail("key file must be root-owned for privileged recovery")
    return candidate


@dataclass(frozen=True, slots=True)
class UnlockPlan:
    command: tuple[str, ...]
    stdin: str | None
    luks_uuid_command: tuple[str, ...]
    read_only: bool

    def public(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "stdin": "provided" if self.stdin is not None else None,
            "luks_uuid_command": list(self.luks_uuid_command),
            "read_only": self.read_only,
        }


def build_unlock_plan(identity: TargetIdentity | Mapping[str, Any], *, key_file: str | Path | None = None, passphrase: str | None = None, mapper: str = RECOVERY_MAPPER, read_only: bool = True) -> UnlockPlan | None:
    target = _identity_from(identity)
    if not target.eligible:
        raise _fail("target identity is not eligible for recovery")
    if not target.encrypted:
        if key_file is not None or passphrase is not None:
            raise _fail("plain-root target does not accept unlock credentials")
        return None
    if target.root_device is None or target.luks_uuid is None:
        raise _fail("encrypted target lacks a LUKS device identity")
    mapper = _validate_mapper(mapper)
    if (key_file is None) == (passphrase is None):
        raise _fail("provide exactly one existing key file or passphrase")
    if key_file is not None:
        credential = _validate_key_file(key_file)
        stdin = None
        command = ("cryptsetup", "open", "--readonly", "--type", "luks2", "--key-file", str(credential), "--", target.root_device, mapper)
    else:
        if not isinstance(passphrase, str) or not passphrase:
            raise _fail("passphrase is empty")
        # cryptsetup --key-file=- consumes the supplied bytes exactly.  Do
        # not append a newline: a newline would be part of the existing key
        # material and cause an otherwise correct passphrase to fail.
        stdin = passphrase
        command = ("cryptsetup", "open", "--readonly", "--type", "luks2", "--key-file=-", "--", target.root_device, mapper)
    if not read_only:
        command = tuple(item for item in command if item != "--readonly")
    return UnlockPlan(command=command, stdin=stdin, luks_uuid_command=("cryptsetup", "luksUUID", "--", target.root_device), read_only=read_only)


@dataclass(frozen=True, slots=True)
class OwnedMapper:
    mapper: str
    runner: Runner
    closed: bool = False

    @property
    def device(self) -> str:
        return f"/dev/mapper/{self.mapper}"

    def close(self) -> None:
        if self.closed:
            return
        _run(self.runner, ["cryptsetup", "close", "--", self.mapper])
        object.__setattr__(self, "closed", True)


def unlock_target(identity: TargetIdentity | Mapping[str, Any], *, key_file: str | Path | None = None, passphrase: str | None = None, mapper: str = RECOVERY_MAPPER, read_only: bool = True, runner: Runner = subprocess.run) -> OwnedMapper | None:
    """Validate the LUKS UUID and open an existing volume.

    Read-only is the default.  A writable mapper is reachable only from the
    separately confirmed bounded repair path.
    """

    target = _identity_from(identity)
    plan = build_unlock_plan(target, key_file=key_file, passphrase=passphrase, mapper=mapper, read_only=read_only)
    if plan is None:
        return None
    uuid_result = _run(runner, plan.luks_uuid_command)
    actual = _clean(_stdout(uuid_result))
    if actual != target.luks_uuid:
        raise _fail("target LUKS UUID changed")
    _run(runner, plan.command, input_text=plan.stdin)
    return OwnedMapper(mapper=_validate_mapper(mapper), runner=runner)


@dataclass(slots=True)
class MountLease:
    root_mount: Path
    boot_mount: Path
    runner: Runner
    mapper: OwnedMapper | None = None
    root_owned: bool = True
    boot_owned: bool = True
    cleaned: bool = False

    def cleanup(self) -> None:
        if self.cleaned:
            return
        failures: list[str] = []
        for owned, path in ((self.boot_owned, self.boot_mount), (self.root_owned, self.root_mount)):
            if not owned:
                continue
            try:
                _run(self.runner, ["umount", "--", str(path)])
                if path == self.boot_mount:
                    self.boot_owned = False
                else:
                    self.root_owned = False
            except RecoveryError:
                failures.append(str(path))
        if self.mapper is not None:
            try:
                self.mapper.close()
            except RecoveryError:
                failures.append(f"/dev/mapper/{self.mapper.mapper}")
        self.cleaned = not failures
        if failures:
            raise _fail("recovery cleanup failed; retained resources: " + ", ".join(failures))

    close = cleanup

    def __enter__(self) -> "MountLease":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.cleanup()


def _mountpoint(path: str | Path, *, description: str, allow_boot_child: bool = False) -> Path:
    candidate = _absolute(path, description=description)
    if candidate == Path("/"):
        raise _fail("recovery mountpoint cannot be the host root")
    _reject_symlink_components(candidate)
    if not candidate.is_dir():
        raise _fail(f"{description} must be an existing directory")
    try:
        info = candidate.stat()
    except OSError as exc:
        raise _fail(f"{description} cannot be inspected") from exc
    if os.geteuid() == 0 and (info.st_uid != 0 or info.st_mode & 0o022):
        raise _fail(f"{description} must be root-owned and private")
    try:
        entries = list(candidate.iterdir())
        if entries and not (allow_boot_child and {entry.name for entry in entries} == {"boot"} and entries[0].is_dir()):
            raise _fail(f"{description} must be empty")
    except OSError as exc:
        raise _fail(f"{description} cannot be inspected") from exc
    return candidate


def mount_target(identity: TargetIdentity | Mapping[str, Any], root_mount: str | Path, boot_mount: str | Path, *, mapper: OwnedMapper | None = None, writable: bool = False, runner: Runner = subprocess.run) -> MountLease:
    """Mount an explicitly selected target, read-only unless repair opted in."""

    target = _identity_from(identity)
    if not target.eligible:
        raise _fail("target identity is not eligible for recovery")
    root_dir = _mountpoint(root_mount, description="root mountpoint", allow_boot_child=Path(root_mount) / "boot" == Path(boot_mount))
    boot_dir = _mountpoint(boot_mount, description="boot mountpoint")
    if root_dir == boot_dir:
        raise _fail("root and boot mountpoints must differ")
    source = mapper.device if mapper is not None else target.root_device
    if source is None or target.boot_device is None:
        raise _fail("target partition paths are unavailable")
    root_options = "rw" if writable else "ro,noload"
    boot_options = "rw,nosuid,nodev,noexec" if writable else "ro,nosuid,nodev,noexec"
    mounted_root = False
    try:
        _run(runner, ["mount", "-o", root_options, "--", source, str(root_dir)])
        mounted_root = True
        _run(runner, ["mount", "-o", boot_options, "--", target.boot_device, str(boot_dir)])
        return MountLease(root_dir, boot_dir, runner, mapper=mapper)
    except RecoveryError:
        if mounted_root:
            try:
                _run(runner, ["umount", "--", str(root_dir)])
            except RecoveryError:
                pass
        raise


@dataclass(frozen=True, slots=True)
class RepairPlan:
    commands: tuple[tuple[str, ...], ...]
    verification_commands: tuple[tuple[str, ...], ...]
    preserved_paths: tuple[str, ...]
    kernel_name: str
    missing_kernel: bool
    rebuild_initramfs: bool = True

    def public(self) -> dict[str, Any]:
        return {
            "commands": [list(command) for command in self.commands],
            "verification_commands": [list(command) for command in self.verification_commands],
            "preserved_paths": list(self.preserved_paths),
            "kernel_name": self.kernel_name,
            "missing_kernel": self.missing_kernel,
            "rebuild_initramfs": self.rebuild_initramfs,
            "scope": "boot and initramfs only",
        }


def _target_root_paths(root: str | Path, boot: str | Path) -> tuple[Path, Path]:
    root_dir = _absolute(root, description="target root")
    boot_dir = _absolute(boot, description="target boot")
    if root_dir == Path("/"):
        raise _fail("refusing the host root as recovery target")
    if boot_dir != root_dir / "boot":
        raise _fail("target boot must be the selected target root's boot directory")
    _reject_symlink_components(root_dir)
    _reject_symlink_components(boot_dir)
    if not root_dir.is_dir() or not boot_dir.is_dir():
        raise _fail("mounted target root and boot are unavailable")
    return root_dir, boot_dir


def _optional_regular(path: Path, *, description: str) -> bool:
    if not os.path.lexists(path):
        return False
    _regular_file(path, description=description, nonempty=False)
    return True


def _configured_kernel(boot: Path) -> str:
    config = _regular_file(boot / "config.txt", description="target boot configuration")
    try:
        lines = config.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise _fail("target boot configuration is unreadable") from exc
    names = [match.group(1) for line in lines if (match := _KERNEL_ASSIGNMENT.fullmatch(line))]
    if len(names) != 1 or not _SAFE_KERNEL.fullmatch(names[0]):
        raise _fail("target boot configuration has no single supported kernel")
    return names[0]


def _kernel_present(boot: Path, name: str) -> bool:
    candidate = boot / name
    if not os.path.lexists(candidate):
        return False
    _regular_file(candidate, description="configured Pi kernel", nonempty=False)
    return candidate.stat().st_size > 0


def _module_tree_present(root: Path) -> bool:
    for base in (root / "usr/lib/modules", root / "lib/modules"):
        if not base.is_dir() or base.is_symlink():
            continue
        if any(item.is_dir() and not item.is_symlink() for item in base.iterdir()):
            return True
    return False


def _nspawn_command(root: Path, boot: Path, command: Sequence[str]) -> tuple[str, ...]:
    return (
        "systemd-nspawn",
        "--quiet",
        "--register=no",
        "--private-users=no",
        "--network-namespace-path=/proc/1/ns/net",
        "--resolv-conf=replace-host",
        "--timezone=off",
        "--pipe",
        f"--bind={boot}:/boot",
        "--directory",
        str(root),
        "--",
        *command,
    )


def _pacman_field(path: Path, field: str) -> str | None:
    """Read one exact field from a target-local pacman ``desc`` file."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise _fail("target package metadata is unreadable") from exc
    marker = f"%{field}%"
    for index, line in enumerate(lines[:-1]):
        if line == marker:
            value = lines[index + 1].strip()
            return value or None
    return None


@dataclass(frozen=True, slots=True)
class CachedKernelPackage:
    archive: Path
    signature: Path
    version: str
    architecture: str


def _validate_cached_package_file(path: Path, *, description: str) -> None:
    _regular_file(path, description=description)
    try:
        info = path.stat()
    except OSError as exc:
        raise _fail(f"{description} cannot be inspected") from exc
    if os.geteuid() == 0 and (info.st_uid != 0 or info.st_mode & 0o022):
        raise _fail(f"{description} has unsafe ownership or permissions")


def _installed_linux_rpi_archive(root: Path) -> CachedKernelPackage:
    """Find the exact cached archive for the installed linux-rpi package.

    Recovery must never ask a live or host repository for a newer kernel.  An
    absent exact archive is therefore a deliberate refusal with reinstall as
    the separate route.
    """

    local = root / "var/lib/pacman/local"
    if not local.is_dir() or local.is_symlink():
        raise _fail("exact installed linux-rpi archive is unavailable; use the separate installer handoff")
    package_versions: list[str] = []
    package_architectures: list[str] = []
    for directory in sorted(local.iterdir(), key=lambda item: item.name):
        if directory.is_dir() and not directory.is_symlink() and directory.name.startswith("linux-rpi-"):
            name = _pacman_field(directory / "desc", "NAME")
            version = _pacman_field(directory / "desc", "VERSION")
            architecture = _pacman_field(directory / "desc", "ARCH")
            if name == "linux-rpi" and version and architecture:
                package_versions.append(version)
                package_architectures.append(architecture)
    if len(package_versions) != 1:
        raise _fail("exact installed linux-rpi archive is unavailable; use the separate installer handoff")
    version = package_versions[0]
    architecture = package_architectures[0]
    cache = root / "var/cache/pacman/pkg"
    if not cache.is_dir() or cache.is_symlink():
        raise _fail("exact installed linux-rpi archive is unavailable; use the separate installer handoff")
    candidates = []
    prefix = f"linux-rpi-{version}-"
    for archive in sorted(cache.iterdir(), key=lambda item: item.name):
        if archive.is_file() and not archive.is_symlink() and not archive.name.endswith(".sig") and archive.name.startswith(prefix) and ".pkg.tar" in archive.name[len(prefix) :]:
            candidates.append(archive)
    if len(candidates) != 1:
        raise _fail("exact installed linux-rpi archive is unavailable; use the separate installer handoff")
    _validate_cached_package_file(candidates[0], description="linux-rpi package archive")
    signature = Path(str(candidates[0]) + ".sig")
    _validate_cached_package_file(signature, description="linux-rpi package signature")
    return CachedKernelPackage(candidates[0], signature, version, architecture)


def plan_boot_repair(root: str | Path, boot: str | Path) -> RepairPlan:
    """Plan a package-kernel/initramfs repair without changing the target."""

    root_dir, boot_dir = _target_root_paths(root, boot)
    cmdline = root_dir / "boot/cmdline.txt"
    config = root_dir / "boot/config.txt"
    crypttab = root_dir / "etc/crypttab"
    if not _optional_regular(cmdline, description="target kernel command line"):
        raise _fail("target kernel command line is missing")
    if not _optional_regular(config, description="target boot configuration"):
        raise _fail("target boot configuration is missing")
    _optional_regular(crypttab, description="target encryption configuration")
    kernel_name = _configured_kernel(boot_dir)
    missing_kernel = not _kernel_present(boot_dir, kernel_name)
    if not _module_tree_present(root_dir):
        raise _fail("target linux-rpi modules are missing")
    preset_dir = root_dir / "etc/mkinitcpio.d"
    if not preset_dir.is_dir():
        raise _fail("target mkinitcpio preset directory is missing")
    presets = sorted(path for path in preset_dir.glob("linux-rpi*.preset") if path.is_file() and not path.is_symlink())
    if not presets:
        raise _fail("target linux-rpi mkinitcpio preset is missing")
    commands: list[tuple[str, ...]] = []
    verification_commands: list[tuple[str, ...]] = []
    if missing_kernel:
        package = _installed_linux_rpi_archive(root_dir)
        archive_inside_target = "/" + package.archive.relative_to(root_dir).as_posix()
        signature_inside_target = "/" + package.signature.relative_to(root_dir).as_posix()
        verification_commands.extend(
            [
                _nspawn_command(root_dir, boot_dir, ("/usr/bin/pacman-key", "--verify", signature_inside_target, archive_inside_target)),
                _nspawn_command(root_dir, boot_dir, ("/usr/bin/pacman", "-Qp", "--print-format", "%n %v %a", "--", archive_inside_target)),
            ]
        )
        commands.append(_nspawn_command(root_dir, boot_dir, ("/usr/bin/pacman", "--noconfirm", "-U", archive_inside_target)))
    commands.append(_nspawn_command(root_dir, boot_dir, ("/usr/bin/mkinitcpio", "-P")))
    commands.append(_nspawn_command(root_dir, boot_dir, ("/usr/bin/lsinitcpio", "-l", "/boot/initramfs-linux.img")))
    return RepairPlan(
        commands=tuple(commands),
        verification_commands=tuple(verification_commands),
        preserved_paths=(str(cmdline), str(config), str(crypttab)) if crypttab.exists() else (str(cmdline), str(config)),
        kernel_name=kernel_name,
        missing_kernel=missing_kernel,
    )


def _hash_paths(paths: Iterable[str]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for raw in paths:
        path = Path(raw)
        if not os.path.lexists(path):
            result[raw] = None
            continue
        _regular_file(path, description="preserved recovery file", nonempty=False)
        result[raw] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _verify_cached_kernel_package(root: Path, boot: Path, plan: RepairPlan, runner: Runner) -> None:
    if not plan.verification_commands:
        return
    package = _installed_linux_rpi_archive(root)
    if len(plan.verification_commands) != 2:
        raise _fail("kernel package verification plan is invalid")
    _run(runner, plan.verification_commands[0])
    metadata = _run(runner, plan.verification_commands[1])
    fields = _stdout(metadata).strip().split()
    if fields != ["linux-rpi", package.version, package.architecture]:
        raise _fail("cached linux-rpi archive does not match the installed package")


def repair_target(root: str | Path, boot: str | Path, *, runner: Runner = subprocess.run) -> dict[str, Any]:
    """Execute only the reviewed boot/initramfs repair plan.

    The caller must already have mounted the target writable through
    :func:`mount_target` after separately confirming the target.  Existing
    command-line, boot, and crypttab content is hashed before and after the
    repair; any change is reported as a failed repair.
    """

    plan = plan_boot_repair(root, boot)
    root_dir, boot_dir = _target_root_paths(root, boot)
    before = _hash_paths(plan.preserved_paths)
    _verify_cached_kernel_package(root_dir, boot_dir, plan, runner)
    for command in plan.commands:
        result = _run(runner, command)
        if command[-3:] == ("/usr/bin/lsinitcpio", "-l", "/boot/initramfs-linux.img") and not _stdout(result).strip():
            raise _fail("repaired initramfs validation returned no module listing")
    after = _hash_paths(plan.preserved_paths)
    if before != after:
        raise _fail("boot repair changed preserved configuration")
    if not _kernel_present(boot_dir, plan.kernel_name):
        raise _fail("configured Pi kernel is still missing after repair")
    initramfs = boot_dir / "initramfs-linux.img"
    _regular_file(initramfs, description="repaired initramfs")
    return {**plan.public(), "preserved_unchanged": True, "initramfs": str(initramfs)}


def reinstall_handoff(identity: TargetIdentity | Mapping[str, Any], *, confirmation: str | None) -> dict[str, Any]:
    """Return a handoff to the existing installer without touching storage."""

    target = _identity_from(identity)
    if confirmation != REINSTALL_CONFIRMATION:
        raise _fail("reinstall handoff requires its separate confirmation")
    if not target.eligible:
        raise _fail("target identity is not eligible for installer handoff")
    return {
        "handoff": "installer-submit",
        "target": target.to_dict(),
        "message": "Use the existing installer flow with a fresh target confirmation; this recovery helper never formats storage.",
        "destructive": True,
    }


def _credential(request: Mapping[str, Any]) -> tuple[str | None, str | None]:
    key_file = request.get("key_file")
    passphrase = request.get("passphrase")
    if key_file is not None and not isinstance(key_file, str):
        raise _fail("key file is invalid")
    if passphrase is not None and not isinstance(passphrase, str):
        raise _fail("passphrase is invalid")
    return key_file, passphrase


def _select_from_request(request: Mapping[str, Any], *, runner: Runner) -> TargetIdentity:
    path = request.get("target")
    confirmation = request.get("target_confirmation")
    if not isinstance(path, str) or not isinstance(confirmation, str):
        raise _fail("an explicit target and confirmation are required")
    return select_target(path, confirmation=confirmation, runner=runner)


def _inspect_mounted_target(root: Path, boot: Path) -> dict[str, Any]:
    """Return bounded facts without exposing file contents or credentials."""

    cmdline = root / "boot/cmdline.txt"
    config = root / "boot/config.txt"
    crypttab = root / "etc/crypttab"
    provenance = root / "usr/lib/omarchy-pi/installer-provenance.json"
    boot_files: list[str] = []
    for entry in sorted(boot.iterdir(), key=lambda item: item.name):
        if entry.is_file() and not entry.is_symlink():
            boot_files.append(entry.name)
    facts: dict[str, Any] = {
        "boot_files": boot_files,
        "cmdline_present": _optional_regular(cmdline, description="target kernel command line"),
        "config_present": _optional_regular(config, description="target boot configuration"),
        "crypttab_present": _optional_regular(crypttab, description="target encryption configuration"),
        "provenance_present": False,
        "read_only": True,
    }
    if provenance.exists():
        _regular_file(provenance, description="target installer provenance")
        try:
            document = json.loads(provenance.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise _fail("target installer provenance is unreadable") from exc
        if isinstance(document, Mapping) and isinstance(document.get("source_revision"), str):
            facts["source_revision"] = document["source_revision"]
            facts["provenance_present"] = True
    return facts


def handle_request(request: Mapping[str, Any], *, runner: Runner = subprocess.run, mount_root: Path | None = None) -> dict[str, Any]:
    """Handle a root-controller recovery request without leaking secrets."""

    if not isinstance(request, Mapping):
        raise _fail("recovery request is not an object")
    action = request.get("action")
    allowed_fields = {
        "recovery-discover": {"action"},
        "recovery-plan": {"action", "target", "target_confirmation", "key_file", "passphrase", "mapper"},
        "recovery-inspect": {"action", "target", "target_confirmation", "key_file", "passphrase", "mapper"},
        "recovery-repair": {"action", "target", "target_confirmation", "key_file", "passphrase", "mapper", "repair_confirmation"},
    }
    if action not in allowed_fields:
        raise _fail("unknown recovery action")
    if set(request) - allowed_fields[action]:
        raise _fail("recovery request contains an unknown field")
    if action == "recovery-discover":
        # Inventory is read-only.  The caller still has to select one of the
        # returned paths and echo its token before any unlock or repair.
        return {"targets": [target.to_dict() for target in discover_targets(runner=runner)], "read_only": True}
    if action == "recovery-plan":
        target = _select_from_request(request, runner=runner)
        key_file, passphrase = _credential(request)
        plan = build_unlock_plan(target, key_file=key_file, passphrase=passphrase, mapper=str(request.get("mapper", RECOVERY_MAPPER)))
        return {
            "target": target.to_dict(),
            "read_only": True,
            "unlock_plan": plan.public() if plan is not None else None,
            "repair_confirmation": REPAIR_CONFIRMATION,
            "scope": "boot and initramfs only",
            "limitations": ["planning does not unlock, mount, repair, reinstall, or provide full-system rollback"],
        }
    if action not in {"recovery-inspect", "recovery-repair"}:
        raise _fail("unknown recovery action")
    if action == "recovery-repair" and request.get("repair_confirmation") != REPAIR_CONFIRMATION:
        raise _fail("boot repair requires its explicit confirmation")
    target = _select_from_request(request, runner=runner)
    key_file, passphrase = _credential(request)
    mapper = _validate_mapper(str(request.get("mapper", RECOVERY_MAPPER)))
    owned_mapper: OwnedMapper | None = None
    lease: MountLease | None = None
    try:
        writable = action == "recovery-repair"
        owned_mapper = unlock_target(target, key_file=key_file, passphrase=passphrase, mapper=mapper, read_only=not writable, runner=runner)
        root = _absolute(mount_root or MOUNT_ROOT, description="recovery mount root")
        _reject_symlink_components(root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_mount = root / f"root-{uuid.uuid4().hex[:12]}"
        # Mount the boot partition below the selected root.  The target root's
        # own /boot directory is the mountpoint after the root mount hides the
        # temporary directory tree created here.
        boot_mount = root_mount / "boot"
        root_mount.mkdir(mode=0o700)
        boot_mount.mkdir(mode=0o700)
        lease = mount_target(target, root_mount, boot_mount, mapper=owned_mapper, writable=writable, runner=runner)
        owned_mapper = None
        if writable:
            result = repair_target(root_mount, boot_mount, runner=runner)
            return {"target": target.to_dict(), "repaired": True, **result}
        return {"target": target.to_dict(), "inspection": _inspect_mounted_target(root_mount, boot_mount), "limitations": ["inspection does not repair or roll back the installed system"]}
    finally:
        if lease is not None:
            lease.cleanup()
        elif owned_mapper is not None:
            owned_mapper.close()


def _controller_call(request: Mapping[str, Any], *, controller: str = CONTROLLER_PATH, runner: Runner = subprocess.run) -> dict[str, Any]:
    encoded = (json.dumps(dict(request), separators=(",", ":")) + "\n")
    # The installed USB image grants only this fixed, argument-free
    # controller through sudo.  Recovery never asks sudo to execute a caller
    # supplied command or to pass a secret in argv.
    result = _run(runner, ["/usr/bin/sudo", "-n", controller], input_text=encoded)
    try:
        response = json.loads(_stdout(result))
    except (TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("recovery controller returned invalid JSON") from exc
    if not isinstance(response, Mapping) or response.get("ok") is not True:
        raise _fail(str(response.get("error", "recovery controller rejected the request")) if isinstance(response, Mapping) else "recovery controller rejected the request")
    return dict(response)


__all__ = [
    "CONTROLLER_PATH",
    "MOUNT_ROOT",
    "OwnedMapper",
    "MountLease",
    "RECOVERY_MAPPER",
    "REINSTALL_CONFIRMATION",
    "REPAIR_CONFIRMATION",
    "RecoveryError",
    "RepairPlan",
    "TargetIdentity",
    "UnlockPlan",
    "build_unlock_plan",
    "discover_targets",
    "handle_request",
    "mount_target",
    "plan_boot_repair",
    "reinstall_handoff",
    "repair_target",
    "select_target",
    "unlock_target",
]
