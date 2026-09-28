#!/usr/bin/env python3
"""Guarded storage operations for the Raspberry Pi disk installer.

The public functions in this module deliberately keep discovery and mutation
separate.  Discovery builds a block-device graph from ``lsblk``, ``findmnt``,
``/proc/swaps`` and sysfs.  A selected identity is then re-read before every
destructive phase.  The mutating context manager owns only the partitions,
mounts and mapper it creates, which makes its cleanup safe after a failed
install.

The module does not provide a loop-device or fixture bypass in production.
Tests replace the command runner and sysfs paths at the subprocess boundary.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import uuid
from typing import Any, Iterator, Mapping, Sequence


class InstallError(Exception):
    """Raised when storage cannot be selected or safely prepared."""


SECTOR_SIZE = 512
MIB = 1024 * 1024
BOOT_SIZE_MIB = 1024
KEY_SIZE_MIB = 256
FIRST_PARTITION_SECTOR = 2048
MIN_ROOT_SIZE_MIB = 4096
MAX_DOS_SECTORS = 0xFFFFFFFF
MAPPER_NAME = "omarchy-pi-install-cryptroot"
MOUNT_BASE = Path("/mnt")
SECRET_BASE = Path("/run")
SYSFS_ROOT = Path("/sys/class/block")
PROC_SWAPS = Path("/proc/swaps")
INSTALLER_MARKER = Path("/usr/lib/omarchy-pi/installer-image.marker")
INSTALLER_MARKER_CONTENT = b"omarchy-pi-installer-image-v1\n"
MODEL_PATH = Path("/proc/device-tree/model")
KEY_LABEL = "OMARCHYKEY"
BOOT_LABEL = "PI-BOOT"
ROOT_LABEL = "OMARCHY-ROOT"
UUID_RE = re.compile(r"^[0-9A-Fa-f-]{8,64}$")
SERIAL_RE = re.compile(r"^[A-Za-z0-9._:+/-]{1,256}$")
BOOT_ORDER_RE = re.compile(r"\bBOOT_ORDER\s*[:=]\s*(0x[0-9A-Fa-f]+)")


def _run(command: Sequence[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one argv vector; no caller is allowed to provide a shell string."""

    if not command or any(not isinstance(part, str) for part in command):
        raise InstallError("invalid command vector")
    try:
        return subprocess.run(
            list(command),
            input=input_text,
            text=True,
            capture_output=True,
            check=check,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if isinstance(exc.stderr, str) else ""
        if input_text and detail:
            detail = detail.replace(input_text, "<redacted>")
        suffix = f": {detail}" if detail else ""
        raise InstallError(f"command failed: {command[0]}{suffix}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(f"command failed to start: {command[0]}") from exc


def _checked(command: Sequence[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = _run(command, input_text=input_text, check=True)
    except InstallError:
        raise
    except Exception as exc:  # pragma: no cover - defensive runner boundary
        raise InstallError(f"command failed: {command[0]}") from exc
    return result


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "yes", "true", "on"}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return default


def _clean_path(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _device_component(value: Any) -> str | None:
    """Return the sysfs device component from a basename or /dev path."""

    cleaned = _clean_path(value)
    if not cleaned:
        return None
    if "/" in cleaned:
        if not cleaned.startswith("/dev/"):
            return None
        cleaned = Path(cleaned).name
    if not cleaned or cleaned in {".", ".."} or "/" in cleaned:
        return None
    return cleaned


def _sysfs_details(kname: str) -> dict[str, Any]:
    root = SYSFS_ROOT / kname
    holders: list[str] = []
    slaves: list[str] = []
    try:
        holders = sorted(entry.name for entry in (root / "holders").iterdir())
    except OSError:
        pass
    try:
        slaves = sorted(entry.name for entry in (root / "slaves").iterdir())
    except OSError:
        pass
    partition = (root / "partition").exists()
    ro = (_read(root / "ro") or "0").strip() == "1"
    return {"available": root.exists(), "holders": holders, "slaves": slaves, "partition": partition, "ro": ro}


def _normalise_node(raw: Mapping[str, Any], parent: str | None = None) -> dict[str, Any] | None:
    node = dict(raw)
    raw_kname = _device_component(node.get("kname"))
    path = _clean_path(node.get("path")) or (f"/dev/{raw_kname}" if raw_kname else None)
    if not path:
        return None
    node["path"] = path
    node["kname"] = _device_component(node.get("kname")) or _device_component(path)
    if not node["kname"]:
        return None
    node["type"] = _clean_path(node.get("type")) or "unknown"
    node["pkname"] = _device_component(node.get("pkname")) or parent
    children: list[dict[str, Any]] = []
    for child in node.get("children", []) or []:
        normalised = _normalise_node(child, node["kname"])
        if normalised is not None:
            children.append(normalised)
    node["children"] = children
    node["mountpoints"] = node.get("mountpoints") or node.get("mountpoint") or []
    if isinstance(node["mountpoints"], str):
        node["mountpoints"] = [node["mountpoints"]]
    node["mountpoints"] = [mount for mount in node["mountpoints"] if mount]
    node["_sysfs"] = _sysfs_details(node["kname"])
    return node


def _flatten(nodes: Sequence[Mapping[str, Any]], parent: str | None = None) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for raw in nodes:
        node = _normalise_node(raw, parent)
        if node is None:
            continue
        flat.append(node)
        flat.extend(_flatten(node["children"], node["kname"]))
    return flat


def _parse_json(result: subprocess.CompletedProcess[str], description: str) -> dict[str, Any]:
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise InstallError(f"{description} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise InstallError(f"{description} returned an invalid document")
    return value


def _findmnt_mounts() -> list[dict[str, Any]]:
    result = _checked([
        "findmnt",
        "--json",
        "--nofsroot",
        "--output",
        "TARGET,SOURCE,FSTYPE,OPTIONS,MAJ:MIN",
    ])
    document = _parse_json(result, "findmnt")
    def flatten(items: Any) -> list[dict[str, Any]]:
        mounts: list[dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            current = dict(item)
            children = current.pop("children", [])
            mounts.append(current)
            mounts.extend(flatten(children))
        return mounts

    return flatten(document.get("filesystems", []))


def _swap_sources() -> set[str]:
    content = _read(PROC_SWAPS)
    if content is None:
        raise InstallError("cannot read /proc/swaps")
    sources: set[str] = set()
    for line in content.splitlines()[1:]:
        fields = line.split()
        if fields:
            sources.add(fields[0])
    return sources


def _udev_properties(path: str) -> dict[str, str]:
    result = _run(["udevadm", "info", "--query=property", "--name", path], check=False)
    if result.returncode != 0:
        return {}
    properties: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            properties[key] = value
    return properties


def _stable_id(node: Mapping[str, Any]) -> str | None:
    serial = _clean_path(node.get("serial"))
    if serial and SERIAL_RE.fullmatch(serial):
        return f"serial:{serial}"
    wwn = _clean_path(node.get("wwn"))
    if wwn and SERIAL_RE.fullmatch(wwn):
        return f"wwn:{wwn}"
    properties = _udev_properties(str(node["path"]))
    for key, prefix in (("ID_SERIAL", "serial"), ("ID_WWN", "wwn")):
        value = properties.get(key)
        if value and SERIAL_RE.fullmatch(value):
            return f"{prefix}:{value}"
    return None


def _major_minor(item: Mapping[str, Any]) -> str | None:
    return _clean_path(item.get("maj:min") or item.get("major_minor") or item.get("MAJ:MIN"))


def _mounted_node(mount: Mapping[str, Any], by_path: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Resolve a findmnt source by path first, then by its device number."""

    source = _clean_path(mount.get("source"))
    if source:
        node = by_path.get(source)
        if node is not None:
            return node
    mount_number = _major_minor(mount)
    if not mount_number:
        return None
    seen: set[str] = set()
    for node in by_path.values():
        path = str(node.get("path"))
        if path in seen:
            continue
        seen.add(path)
        if _major_minor(node) == mount_number:
            return node
    return None


def _node_path_index(nodes: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for node in nodes:
        path = str(node["path"])
        index[path] = node
        kname = _clean_path(node.get("kname"))
        if kname:
            index[f"/dev/{kname}"] = node
    return index


def _physical_ancestor(node: Mapping[str, Any], by_path: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any] | None:
    current: Mapping[str, Any] | None = node
    visited: set[str] = set()
    while current is not None:
        path = str(current["path"])
        if path in visited:
            return None
        visited.add(path)
        if current.get("type") == "disk":
            return current
        parent = _clean_path(current.get("pkname"))
        current = by_path.get(parent or "") or by_path.get(f"/dev/{parent}" if parent else "")
    return None


def _descendants(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for child in node.get("children", []) or []:
        result.append(child)
        result.extend(_descendants(child))
    return result


def _preflight_error() -> str | None:
    if os.geteuid() != 0:
        return "disk installation requires root"
    machine = _checked(["uname", "-m"]).stdout.strip().lower()
    if machine not in {"aarch64", "arm64"}:
        return f"installer requires native aarch64, found {machine or 'unknown'}"
    try:
        marker = INSTALLER_MARKER.read_bytes()
    except OSError:
        return "installer image marker is missing"
    if marker != INSTALLER_MARKER_CONTENT:
        return "installer image marker is invalid"
    model = _read(MODEL_PATH)
    if not model or "Raspberry Pi 5" not in model:
        return "installer requires a Raspberry Pi 5"
    result = _run(["vcgencmd", "bootloader_config"], check=False)
    if result.returncode != 0:
        return "cannot read EEPROM boot order"
    match = BOOT_ORDER_RE.search(result.stdout)
    if not match:
        return "EEPROM boot order is unavailable"
    # Raspberry Pi encodes the first attempted medium in the rightmost
    # hexadecimal digit.  Accept USB-first orders with either NVMe or SD
    # fallback (for example 0xf164 and 0xf64), while rejecting NVMe-first.
    order_digits = [digit for digit in match.group(1)[2:].lower()[::-1] if digit != "f"]
    if not order_digits or order_digits[0] != "4" or "6" not in order_digits[1:]:
        return "EEPROM USB-first boot order is required"
    return None


def _debugfs_has_path(item: Mapping[str, Any], path: str) -> bool:
    if (_clean_path(item.get("fstype")) or "").lower() != "ext4" or item.get("mountpoints"):
        return False
    try:
        result = _run(["debugfs", "-R", f"stat {path}", "--", str(item["path"])], check=False)
    except InstallError:
        return False
    return result.returncode == 0 and "Inode:" in result.stdout


def _key_marker(node: Mapping[str, Any]) -> bool:
    for item in [node, *_descendants(node)]:
        label = (_clean_path(item.get("label")) or "").upper()
        if label == KEY_LABEL:
            return True
        # A key stick can lose its label while retaining the protected file.
        # debugfs opens ext4 read-only by default; no filesystem mount or
        # write flag is permitted here.  Restrict the probe to unmounted ext4
        # partitions so a running root filesystem is never inspected this way.
        if _debugfs_has_path(item, "/.cryptroot.key"):
            return True
    return False


def _installer_marker(node: Mapping[str, Any]) -> bool:
    for item in [node, *_descendants(node)]:
        label = (_clean_path(item.get("label")) or "").upper()
        if label == "OMARCHY-INSTALLER" or _debugfs_has_path(item, "/usr/lib/omarchy-pi/installer-image.marker"):
            return True
    return False


def _reason_mounts(node: Mapping[str, Any], by_path: Mapping[str, Mapping[str, Any]], mounts: Sequence[Mapping[str, Any]]) -> list[str]:
    reasons: list[str] = []
    descendants = [node, *_descendants(node)]
    if any(item.get("mountpoints") for item in descendants):
        reasons.append("mounted filesystem or descendant")
    for mount in mounts:
        source = _clean_path(mount.get("source")) or _major_minor(mount) or "unknown"
        source_node = _mounted_node(mount, by_path)
        if source_node is not None and _physical_ancestor(source_node, by_path) is node:
            reasons.append(f"mounted source {source}")
    return reasons


def _graph() -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str], str | None]:
    result = _checked([
        "lsblk",
        "--json",
        "--tree",
        "--paths",
        "--bytes",
        "--output",
        "PATH,KNAME,TYPE,SIZE,MODEL,SERIAL,WWN,TRAN,FSTYPE,LABEL,UUID,PARTUUID,MOUNTPOINTS,PKNAME,RO,RM,HOTPLUG,STATE,MAJ:MIN,LOG-SEC,PTTYPE",
    ])
    document = _parse_json(result, "lsblk")
    nodes = _flatten(document.get("blockdevices", []))
    mounts = _findmnt_mounts()
    swaps = _swap_sources()
    by_path = _node_path_index(nodes)
    protected_swap: set[str] = set()
    for source in swaps:
        source_node = by_path.get(source)
        if source_node is not None:
            ancestor = _physical_ancestor(source_node, by_path)
            if ancestor:
                protected_swap.add(str(ancestor["path"]))
    protected_mounts = set()
    for mount in mounts:
        source_node = _mounted_node(mount, by_path)
        if source_node is not None:
            ancestor = _physical_ancestor(source_node, by_path)
            if ancestor:
                protected_mounts.add(str(ancestor["path"]))
    return nodes, mounts, protected_swap | protected_mounts, _preflight_error()


def _identity_for(node: Mapping[str, Any], *, nodes: Sequence[Mapping[str, Any]], protected: set[str], preflight_error: str | None, mounts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_path = _node_path_index(nodes)
    path = str(node["path"])
    stable_id = _stable_id(node)
    reasons: list[str] = []
    descendants = _descendants(node)
    if node.get("type") != "disk":
        reasons.append("not a whole physical disk")
    if path.startswith("/dev/loop") or node.get("type") in {"loop", "rom", "dm", "crypt", "md", "lvm"}:
        reasons.append("virtual or non-writable device")
    if _bool(node.get("ro")) or _bool(node.get("_sysfs", {}).get("ro")):
        reasons.append("read-only device")
    if not stable_id:
        reasons.append("no stable serial, WWN, or udev path identity")
    if not _int(node.get("size")):
        reasons.append("unknown device size")
    if _int(node.get("size")) < (BOOT_SIZE_MIB + MIN_ROOT_SIZE_MIB) * MIB:
        reasons.append("device is too small")
    logical_sector = _int(node.get("log-sec") or node.get("log_sec"))
    if logical_sector != 512:
        reasons.append("logical sector size is not 512 bytes")
    if _int(node.get("size")) // SECTOR_SIZE > MAX_DOS_SECTORS:
        reasons.append("device exceeds the DOS/MBR 2 TiB limit")
    node_fstype = _clean_path(node.get("fstype"))
    node_label = _clean_path(node.get("label"))
    node_uuid = _clean_path(node.get("uuid"))
    node_pttype = _clean_path(node.get("pttype")) or _clean_path(node.get("PTTYPE"))
    if any(not item.get("_sysfs", {}).get("available") for item in [node, *descendants]):
        reasons.append("sysfs block graph is unavailable")
    if any(item.get("_sysfs", {}).get("holders") or item.get("_sysfs", {}).get("slaves") for item in [node, *descendants]):
        reasons.append("device has active sysfs holders or slaves")
    reasons.extend(_reason_mounts(node, by_path, mounts))
    if path in protected:
        reasons.append("contains a mounted root, boot, or active swap")
    key_media = _key_marker(node)
    installer_media = _installer_marker(node)
    if key_media:
        reasons.append("protected existing unlock-key media")
    if installer_media:
        reasons.append("installer media")
    if preflight_error:
        reasons.append(preflight_error)
    # A removable USB is valid as a key candidate only when it is genuinely
    # blank.  It remains ineligible as a target once selected as key media.
    blank_candidate = (
        node.get("type") == "disk"
        and str(node.get("tran") or "").lower() == "usb"
        and not descendants
        and not key_media
        and not installer_media
        and not node_fstype
        and not node_label
        and not node_uuid
        and not node_pttype
        and not any(reason not in {"device is too small"} for reason in reasons)
        and _int(node.get("size")) >= FIRST_PARTITION_SECTOR * SECTOR_SIZE + KEY_SIZE_MIB * MIB
    )
    return {
        "path": path,
        "identity": {
            "stable_id": stable_id,
            "serial": _clean_path(node.get("serial")),
            "wwn": _clean_path(node.get("wwn")),
            "model": _clean_path(node.get("model")),
            "transport": _clean_path(node.get("tran")),
            "size": _int(node.get("size")),
            "logical_sector": logical_sector,
            "partition_table": node_pttype,
            "major_minor": _clean_path(node.get("maj:min")),
            "partitions": [str(item["path"]) for item in descendants if item.get("type") == "part"],
            "key_media": key_media,
            "installer_media": installer_media,
            "blank_key_candidate": blank_candidate,
        },
        "size": _int(node.get("size")),
        "eligible": not reasons,
        "key_eligible": blank_candidate,
        "reasons": reasons,
    }


def discover_disks() -> list[dict[str, Any]]:
    """Return physical disk identities and all reasons a disk is refused."""

    nodes, mounts, protected, preflight_error = _graph()
    disks = [node for node in nodes if node.get("type") == "disk"]
    return [
        _identity_for(
            node,
            nodes=nodes,
            protected=protected,
            preflight_error=preflight_error,
            mounts=mounts,
        )
        for node in disks
    ]


def _same_identity(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_id = left.get("identity", {}).get("stable_id") if isinstance(left.get("identity"), Mapping) else left.get("stable_id")
    right_id = right.get("identity", {}).get("stable_id") if isinstance(right.get("identity"), Mapping) else right.get("stable_id")
    return bool(left_id and left_id == right_id)


def _require_preflight() -> None:
    error = _preflight_error()
    if error:
        raise InstallError(error)


def select_disk(path: str) -> dict[str, Any]:
    """Select one currently eligible whole disk by its current device path."""

    _require_preflight()
    requested = os.path.realpath(path)
    candidates = discover_disks()
    for candidate in candidates:
        if os.path.realpath(str(candidate["path"])) != requested:
            continue
        if not candidate["eligible"]:
            raise InstallError(f"refusing {path}: {'; '.join(candidate['reasons'])}")
        return candidate
    raise InstallError(f"disk is not a discovered whole physical device: {path}")


def select_key_disk(path: str) -> dict[str, Any]:
    """Select a fresh blank USB identity for key preparation."""

    _require_preflight()
    requested = os.path.realpath(path)
    candidates = discover_disks()
    for candidate in candidates:
        if os.path.realpath(str(candidate["path"])) != requested:
            continue
        if not candidate.get("key_eligible"):
            reasons = "; ".join(candidate.get("reasons", [])) or "not a fresh blank key USB"
            raise InstallError(f"refusing key medium {path}: {reasons}")
        return candidate
    raise InstallError(f"key medium is not a discovered whole physical device: {path}")


def confirm_token(identity: Mapping[str, Any]) -> str:
    """Return the exact token the frontend must display and request."""

    stable_id = identity.get("identity", {}).get("stable_id") if isinstance(identity.get("identity"), Mapping) else identity.get("stable_id")
    path = identity.get("path")
    if not isinstance(path, str) or not stable_id:
        raise InstallError("cannot create a confirmation token without stable identity")
    return f"ERASE {path} {stable_id}"


def _current_identity(identity: Mapping[str, Any], *, role: str) -> dict[str, Any]:
    stable_id = identity.get("identity", {}).get("stable_id") if isinstance(identity.get("identity"), Mapping) else identity.get("stable_id")
    if not stable_id:
        raise InstallError(f"{role} has no stable identity")
    current = discover_disks()
    matches = [candidate for candidate in current if candidate.get("identity", {}).get("stable_id") == stable_id]
    if len(matches) > 1:
        raise InstallError(f"{role} stable identity is ambiguous")
    if not matches:
        raise InstallError(f"{role} identity disappeared or changed")
    candidate = matches[0]
    old_details = identity.get("identity", {}) if isinstance(identity.get("identity"), Mapping) else identity
    old_size = identity.get("size") or old_details.get("size")
    if old_size and int(old_size) != int(candidate["size"]):
        raise InstallError(f"{role} size changed")
    if identity.get("path") and identity.get("path") != candidate.get("path"):
        raise InstallError(f"{role} device path changed")
    current_details = candidate.get("identity", {})
    for field in ("major_minor", "model", "transport"):
        old_value = old_details.get(field)
        if old_value and old_value != current_details.get(field):
            raise InstallError(f"{role} {field} changed")
    return candidate


def _revalidate(identity: Mapping[str, Any], *, role: str) -> dict[str, Any]:
    current = _current_identity(identity, role=role)
    if role == "target" and not current["eligible"]:
        raise InstallError(f"target changed: {'; '.join(current['reasons'])}")
    if role == "key" and not current.get("key_eligible"):
        raise InstallError("key media is no longer a blank USB stick")
    return current


def _validate_key(identity: Mapping[str, Any]) -> dict[str, Any]:
    current = _revalidate(identity, role="key")
    details = current["identity"]
    if details.get("key_media"):
        raise InstallError("refusing an existing unlock-key USB; choose a fresh blank stick")
    if not details.get("blank_key_candidate"):
        raise InstallError("key USB must be blank, unmounted, and unmarked")
    if int(current["size"]) < (FIRST_PARTITION_SECTOR * SECTOR_SIZE + KEY_SIZE_MIB * MIB):
        raise InstallError("key USB is too small for its 256 MiB key partition")
    return current


def validate_pair(target_identity: Mapping[str, Any], key_identity: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Revalidate selected target/key identities immediately before mutation."""

    _require_preflight()
    target = _revalidate(target_identity, role="target")
    key = _validate_key(key_identity) if key_identity is not None else None
    if key is not None and _same_identity(target, key):
        raise InstallError("target and key media must be different devices")
    return target, key


def _partition_devices(path: str) -> tuple[str, str]:
    name = Path(path).name
    if name.startswith("nvme") or name.startswith("mmcblk") or name.startswith("loop"):
        return f"{path}p1", f"{path}p2"
    return f"{path}1", f"{path}2"


def _partition_script(size_bytes: int) -> str:
    if size_bytes <= 0 or size_bytes // SECTOR_SIZE > MAX_DOS_SECTORS:
        raise InstallError("target does not fit the DOS/MBR 2 TiB limit")
    total_sectors = size_bytes // SECTOR_SIZE
    boot_sectors = BOOT_SIZE_MIB * MIB // SECTOR_SIZE
    root_start = FIRST_PARTITION_SECTOR + boot_sectors
    root_sectors = total_sectors - root_start
    if root_sectors < MIN_ROOT_SIZE_MIB * MIB // SECTOR_SIZE:
        raise InstallError("target does not have enough aligned space for the root filesystem")
    return (
        "label: dos\n"
        "unit: sectors\n"
        f"first-lba: {FIRST_PARTITION_SECTOR}\n"
        f"{FIRST_PARTITION_SECTOR},{boot_sectors},c,*\n"
        f"{root_start},{root_sectors},83\n"
    )


def _key_partition_script(size_bytes: int) -> str:
    if size_bytes <= 0 or size_bytes // SECTOR_SIZE > MAX_DOS_SECTORS:
        raise InstallError("key USB does not fit the DOS/MBR 2 TiB limit")
    total_sectors = size_bytes // SECTOR_SIZE
    key_sectors = KEY_SIZE_MIB * MIB // SECTOR_SIZE
    if total_sectors < FIRST_PARTITION_SECTOR + key_sectors:
        raise InstallError("key USB is too small for its aligned 256 MiB partition")
    return (
        "label: dos\n"
        "unit: sectors\n"
        f"first-lba: {FIRST_PARTITION_SECTOR}\n"
        f"{FIRST_PARTITION_SECTOR},{key_sectors},83\n"
    )


def _safe_mount_dir(prefix: str) -> Path:
    MOUNT_BASE.mkdir(mode=0o755, parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=MOUNT_BASE))


def _safe_secret_dir(prefix: str) -> Path:
    SECRET_BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=SECRET_BASE))
    os.chmod(path, 0o700)
    return path


def _reread_partitions(device: str) -> None:
    _checked(["partprobe", "--", device])
    _checked(["udevadm", "settle"])


def _uuid_for(device: str) -> str:
    result = _checked(["blkid", "-s", "UUID", "-o", "value", "--", device])
    value = result.stdout.strip()
    if not UUID_RE.fullmatch(value):
        raise InstallError(f"filesystem UUID is invalid for {device}")
    return value


def _luks_uuid_for(device: str) -> str:
    result = _checked(["cryptsetup", "luksUUID", "--", device])
    value = result.stdout.strip()
    if not UUID_RE.fullmatch(value):
        raise InstallError(f"LUKS UUID is invalid for {device}")
    return value


def _write_secret_file(directory: Path, name: str, value: bytes) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o400)
    try:
        os.write(descriptor, value)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o400)
    return path


def _verify_key_file(path: Path, expected: bytes) -> None:
    info = path.stat()
    if info.st_size != 64 or (info.st_mode & 0o777) != 0o400 or info.st_uid != 0 or info.st_gid != 0:
        raise InstallError("generated key file has unsafe size, ownership, or mode")
    if path.read_bytes() != expected:
        raise InstallError("generated key file failed read-back verification")


def _make_key(key_identity: Mapping[str, Any], *, job_dir: Path) -> tuple[str, str, Path, Path]:
    key_disk = str(key_identity["path"])
    key_part, _unused = _partition_devices(key_disk)
    current = _revalidate(key_identity, role="key")
    _checked(["wipefs", "--all", "--force", "--", key_disk])
    # wipefs removes the old partition/filesystem signatures.  Let udev
    # publish that change before rediscovering the identity; otherwise a
    # transient stale lsblk/sysfs graph can look like device replacement.
    _checked(["udevadm", "settle", "--timeout=30"])
    current = _current_identity(current, role="key")
    if current["path"] != key_disk:
        raise InstallError("key device path changed during preparation")
    _checked(["sfdisk", "--no-reread", "--", key_disk], input_text=_key_partition_script(int(current["size"])))
    _reread_partitions(key_disk)
    current = _current_identity(current, role="key")
    if current["path"] != key_disk:
        raise InstallError("key device path changed after partitioning")
    key_part, _unused = _partition_devices(key_disk)
    current = _current_identity(current, role="key")
    if current["path"] != key_disk:
        raise InstallError("key device path changed before filesystem formatting")
    _checked(["mkfs.ext4", "-F", "-U", "random", "-L", KEY_LABEL, "--", key_part])
    key_uuid = _uuid_for(key_part)
    key_mount = _safe_mount_dir(f"omarchy-pi-key-{uuid.uuid4().hex[:12]}-")
    mounted = False
    try:
        _checked(["mount", "--", key_part, os.fspath(key_mount)])
        mounted = True
        key_path = key_mount / ".cryptroot.key"
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        key_bytes = secrets.token_bytes(64)
        try:
            os.write(descriptor, key_bytes)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(key_path, 0o400)
        try:
            os.chown(key_path, 0, 0)
        except PermissionError:
            # Production runs as root.  Tests may run unprivileged; ownership
            # is still checked by the production path before returning.
            if os.geteuid() == 0:
                raise
        _verify_key_file(key_path, key_bytes)
        _checked(["sync"])
        _checked(["umount", "--", os.fspath(key_mount)])
        mounted = False
        # Reopen the filesystem read-only and verify the file after the
        # unmount/remount boundary.  This proves that the bytes and metadata
        # came from the newly formatted key medium, rather than only from the
        # writer's page cache.
        _checked(["mount", "-o", "ro", "--", key_part, os.fspath(key_mount)])
        mounted = True
        _verify_key_file(key_mount / ".cryptroot.key", key_bytes)
        _checked(["umount", "--", os.fspath(key_mount)])
        mounted = False
        key_file = _write_secret_file(job_dir, "generated-key", key_bytes)
        return key_part, key_uuid, key_file, key_mount
    finally:
        cleanup_failures: list[str] = []
        if mounted:
            if _cleanup_command(["umount", "--", os.fspath(key_mount)]):
                mounted = False
            else:
                cleanup_failures.append(os.fspath(key_mount))
        if not mounted and not _cleanup_tree(key_mount):
            cleanup_failures.append(os.fspath(key_mount))
        if cleanup_failures:
            raise InstallError(
                "cleanup failed; retained resources: " + ", ".join(dict.fromkeys(cleanup_failures))
            )


def _cleanup_command(command: Sequence[str]) -> bool:
    try:
        return _run(command, check=False).returncode == 0
    except InstallError:
        return False


def _cleanup_unlink(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _cleanup_tree(path: Path) -> bool:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return True
    except (OSError, shutil.Error):
        return False
    return True


@contextmanager
def prepare_target(
    target_identity: Mapping[str, Any],
    mode: str,
    passphrase: str | None,
    key_identity: Mapping[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Prepare and mount one target, yielding only resources owned by the job."""

    if mode not in {"plain", "passphrase", "key"}:
        raise InstallError("mode must be plain, passphrase, or key")
    if mode == "plain" and (passphrase or key_identity is not None):
        raise InstallError("plain mode cannot receive encryption credentials")
    if mode in {"passphrase", "key"} and (
        not isinstance(passphrase, str)
        or not passphrase
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in passphrase)
    ):
        raise InstallError("encrypted modes require a nonempty recovery passphrase")
    if mode == "key" and key_identity is None:
        raise InstallError("key mode requires a separately selected key USB")
    if mode != "key" and key_identity is not None:
        raise InstallError("a key USB is valid only in key mode")

    target, key = validate_pair(target_identity, key_identity)
    # Recheck once more directly before the first destructive command.  This
    # catches a device replacement between validation and command dispatch.
    target, key = validate_pair(target, key)
    target_path = str(target["path"])
    boot_device, root_device = _partition_devices(target_path)
    mount_dir = _safe_mount_dir(f"omarchy-pi-install-{uuid.uuid4().hex[:12]}-")
    root_mount = mount_dir / "root"
    boot_mount = root_mount / "boot"
    root_mount.mkdir()
    job_dir = _safe_secret_dir(f"omarchy-pi-install-{uuid.uuid4().hex[:12]}-")
    mapper_name = f"{MAPPER_NAME}-{uuid.uuid4().hex[:12]}"
    mapper_path = f"/dev/mapper/{mapper_name}"
    root_mounted = False
    boot_mounted = False
    mapper_open = False
    key_part: str | None = None
    key_uuid: str | None = None
    key_file: Path | None = None
    key_mount: Path | None = None
    try:
        # Prepare a fresh key stick first.  If this fails, the target remains
        # untouched; a completed key preparation is deliberately retained for
        # the operator to inspect if a later target phase fails.
        if mode == "key":
            if key is None:
                raise InstallError("key identity disappeared")
            key_part, key_uuid, key_file, key_mount = _make_key(key, job_dir=job_dir)

        _checked(["wipefs", "--all", "--force", "--", target_path])
        # As above, settle the kernel/udev view before the post-wipe identity
        # guard.  The guard remains mandatory; this only removes the expected
        # partition-signature propagation race.
        _checked(["udevadm", "settle", "--timeout=30"])
        # Re-run the identity guard after wipefs and before partitioning.  The
        # command itself is destructive, so later retries still fail closed.
        current_target = _revalidate(target, role="target")
        if current_target["path"] != target_path:
            raise InstallError("target device path changed during preparation")
        _checked(["sfdisk", "--no-reread", "--", target_path], input_text=_partition_script(int(target["size"])))
        _reread_partitions(target_path)
        current_target = _revalidate(target, role="target")
        if current_target["path"] != target_path:
            raise InstallError("target device path changed after partitioning")
        _checked(["mkfs.fat", "-F", "32", "-n", BOOT_LABEL, "--", boot_device])

        if mode == "plain":
            current_target = _revalidate(target, role="target")
            if current_target["path"] != target_path:
                raise InstallError("target device path changed before root formatting")
            _checked(["mkfs.ext4", "-F", "-U", "random", "-L", ROOT_LABEL, "--", root_device])
            root_source = root_device
        else:
            current_target = _revalidate(target, role="target")
            if current_target["path"] != target_path:
                raise InstallError("target device path changed before encryption")
            recovery_file = _write_secret_file(job_dir, "recovery-passphrase", (passphrase or "").encode())
            _checked([
                "cryptsetup",
                "luksFormat",
                "--type", "luks2",
                "--batch-mode",
                "--key-file=-",
                "--",
                root_device,
            ], input_text=passphrase)
            if mode == "key":
                if key is None or key_file is None:
                    raise InstallError("key identity disappeared")
                current_target = _revalidate(target, role="target")
                current_key = _current_identity(key, role="prepared key")
                if current_target["path"] != target_path or current_key["path"] != key["path"]:
                    raise InstallError("device identity changed before key enrollment")
                _checked([
                    "cryptsetup", "luksAddKey", "--batch-mode",
                    "--key-file", os.fspath(recovery_file),
                    "--new-keyfile", os.fspath(key_file), "--", root_device,
                ])
            root_source = mapper_path
            # The open operation below is intentionally repeated after the
            # key enrollment branch; it validates the selected unlock route.

        if mode != "plain":
            current_target = _revalidate(target, role="target")
            if current_target["path"] != target_path:
                raise InstallError("target device path changed before unlock verification")
            _checked(["cryptsetup", "open", "--test-passphrase", "--key-file", os.fspath(recovery_file), "--", root_device])
            if mode == "key":
                if key_file is None:
                    raise InstallError("key file was not created")
                _checked(["cryptsetup", "open", "--test-passphrase", "--key-file", os.fspath(key_file), "--", root_device])
                _checked(["cryptsetup", "open", "--type", "luks2", "--key-file", os.fspath(key_file), "--", root_device, mapper_name])
            else:
                _checked(["cryptsetup", "open", "--type", "luks2", "--key-file", os.fspath(recovery_file), "--", root_device, mapper_name])
            mapper_open = True
            _checked(["mkfs.ext4", "-F", "-U", "random", "-L", ROOT_LABEL, "--", mapper_path])
            root_source = mapper_path

        _checked(["mount", "--", root_source, os.fspath(root_mount)])
        root_mounted = True
        # The root mount hides the pre-mount directory tree.  Create /boot
        # only after the target root is mounted, so the FAT mount is attached
        # inside the target filesystem rather than the host-side staging dir.
        boot_mount.mkdir()
        _checked([
            "mount",
            "-t",
            "vfat",
            "-o",
            "fmask=0133,dmask=0022",
            "--",
            boot_device,
            os.fspath(boot_mount),
        ])
        boot_mounted = True
        root_uuid = _uuid_for(root_source)
        boot_uuid = _uuid_for(boot_device)
        if mode == "key" and key_uuid is None:
            raise InstallError("key UUID was not discovered")
        yield {
            "root": root_mount,
            "boot": boot_mount,
            "root_uuid": root_uuid,
            "boot_uuid": boot_uuid,
            "luks_uuid": _luks_uuid_for(root_device) if mode != "plain" else None,
            "key_uuid": key_uuid,
            "key_path": "/.cryptroot.key" if mode == "key" else None,
        }
    except Exception:
        raise
    finally:
        cleanup_ok = True
        cleanup_failures: list[str] = []
        if boot_mounted:
            cleanup_ok = _cleanup_command(["umount", "--", os.fspath(boot_mount)])
            if not cleanup_ok:
                cleanup_failures.append(os.fspath(boot_mount))
        if root_mounted:
            if cleanup_ok:
                cleanup_ok = _cleanup_command(["umount", "--", os.fspath(root_mount)])
                if not cleanup_ok:
                    cleanup_failures.append(os.fspath(root_mount))
            else:
                cleanup_ok = False
                cleanup_failures.append(os.fspath(root_mount))
        if mapper_open:
            if cleanup_ok and re.fullmatch(r"omarchy-pi-install-cryptroot-[0-9a-f]{12}", mapper_name):
                cleanup_ok = _cleanup_command(["cryptsetup", "close", "--", mapper_name])
                if not cleanup_ok:
                    cleanup_failures.append(mapper_path)
            else:
                cleanup_ok = False
                cleanup_failures.append(mapper_path)
        if key_mount is not None and key_mount.exists():
            if cleanup_ok:
                cleanup_ok = _cleanup_command(["umount", "--", os.fspath(key_mount)])
                if not cleanup_ok:
                    cleanup_failures.append(os.fspath(key_mount))
            else:
                cleanup_ok = False
                cleanup_failures.append(os.fspath(key_mount))
        if cleanup_ok:
            if key_file is not None and not _cleanup_unlink(key_file):
                cleanup_ok = False
                cleanup_failures.append(os.fspath(key_file))
            if cleanup_ok and not _cleanup_tree(job_dir):
                cleanup_ok = False
                cleanup_failures.append(os.fspath(job_dir))
            if cleanup_ok and not _cleanup_tree(mount_dir):
                cleanup_ok = False
                cleanup_failures.append(os.fspath(mount_dir))
        if not cleanup_ok:
            # Keep all still-present staging paths when any resource could not
            # be safely released.  In particular, do not recursively delete
            # a tree after an unmount or mapper-close failure.
            for retained in (job_dir, mount_dir):
                if os.path.lexists(os.fspath(retained)):
                    cleanup_failures.append(os.fspath(retained))
        if cleanup_failures:
            raise InstallError(
                "cleanup failed; retained resources: " + ", ".join(dict.fromkeys(cleanup_failures))
            )


__all__ = [
    "InstallError",
    "discover_disks",
    "select_disk",
    "select_key_disk",
    "confirm_token",
    "validate_pair",
    "prepare_target",
]
