"""Parse the small, user-editable installer settings file.

The settings file lives on the installer image's FAT partition.  It is input
data only: this module deliberately decodes TOML and validates the resulting
values without evaluating any part of the document as Python or shell code.
The returned dataclasses are immutable and hide secret values from their
representations so a caller can safely include the top-level result in a
diagnostic message.
"""

from __future__ import annotations

import base64
import binascii
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


MAX_DOCUMENT_BYTES = 64 * 1024
MAX_HOSTNAME_BYTES = 63
MAX_USERNAME_BYTES = 32
MAX_SSID_BYTES = 32
MAX_WIFI_PASSWORD_BYTES = 64
MAX_LOGIN_PASSWORD_BYTES = 256
MAX_RDP_PASSWORD_BYTES = 128
MAX_AUTHORIZED_KEY_BYTES = 8192

_ROOT_KEYS = frozenset({"installer", "wifi", "ssh", "rdp"})
_INSTALLER_KEYS = frozenset({"hostname", "username"})
_WIFI_KEYS = frozenset({"country", "ssid", "password"})
_SSH_KEYS = frozenset({"authorized_key", "password"})
_RDP_KEYS = frozenset({"password"})

_HOSTNAME_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_USERNAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
_RESERVED_USERNAMES = frozenset({"root", "alarm", "nobody"})
_COUNTRY = re.compile(r"[A-Z]{2}\Z")
_HEX_PSK = re.compile(r"[0-9A-Fa-f]{64}\Z")
_SSH_KEY_TYPES = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }
)


class SettingsError(ValueError):
    """Raised when an installer settings document is not valid.

    Messages intentionally contain field names and general reasons only.  In
    particular, they never include the offending value because values include
    Wi-Fi and login secrets.
    """


@dataclass(frozen=True, slots=True)
class InstallerIdentity:
    hostname: str
    username: str


@dataclass(frozen=True, slots=True)
class WifiSettings:
    country: str
    ssid: str
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SSHSettings:
    authorized_key: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)

    @property
    def login_password(self) -> str | None:
        """Compatibility name for first-boot account provisioning callers."""

        return self.password


@dataclass(frozen=True, slots=True)
class RdpSettings:
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class InstallerSettings:
    installer: InstallerIdentity
    wifi: WifiSettings | None
    ssh: SSHSettings
    rdp: RdpSettings

    @property
    def hostname(self) -> str:
        return self.installer.hostname

    @property
    def username(self) -> str:
        return self.installer.username


def _fail(field_name: str, reason: str) -> None:
    raise SettingsError(f"invalid installer settings field {field_name}: {reason}")


def _check_control_characters(field_name: str, value: str) -> None:
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        _fail(field_name, "contains a control character")


def _text(
    table: dict[str, Any],
    field_name: str,
    *,
    required: bool = True,
    minimum: int = 1,
    maximum: int,
    byte_limit: bool = True,
) -> str | None:
    if field_name not in table:
        if required:
            _fail(field_name, "is required")
        return None
    value = table[field_name]
    if not isinstance(value, str):
        _fail(field_name, "must be a string")
    _check_control_characters(field_name, value)
    try:
        size = len(value.encode("utf-8")) if byte_limit else len(value)
    except UnicodeEncodeError:
        _fail(field_name, "is not valid UTF-8")
    if size < minimum or size > maximum:
        _fail(field_name, "has an invalid length")
    return value


def _table(document: dict[str, Any], name: str, keys: frozenset[str], *, required: bool) -> dict[str, Any] | None:
    if name not in document:
        if required:
            _fail(name, "table is required")
        return None
    value = document[name]
    if not isinstance(value, dict):
        _fail(name, "must be a table")
    unknown = set(value) - keys
    if unknown:
        # Do not include unknown key text: a TOML key can itself contain data
        # copied from an accidentally exposed value.
        _fail(name, "contains an unknown key")
    return value


def _hostname(value: str) -> str:
    size = len(value.encode("utf-8"))
    if size > MAX_HOSTNAME_BYTES or not _HOSTNAME_LABEL.fullmatch(value):
        _fail("installer.hostname", "has an invalid name")
    return value


def _username(value: str) -> str:
    if (
        len(value.encode("utf-8")) > MAX_USERNAME_BYTES
        or not _USERNAME.fullmatch(value)
        or value in _RESERVED_USERNAMES
    ):
        _fail("installer.username", "has an invalid name")
    return value


def _wifi(table: dict[str, Any] | None) -> WifiSettings | None:
    if table is None:
        return None
    country = _text(table, "country", maximum=2, byte_limit=False)
    if not _COUNTRY.fullmatch(country):
        _fail("wifi.country", "must be an uppercase two-letter country code")
    ssid = _text(table, "ssid", maximum=MAX_SSID_BYTES)
    password = _text(table, "password", maximum=MAX_WIFI_PASSWORD_BYTES)
    if not (8 <= len(password.encode("utf-8")) <= 63 and all(0x20 <= ord(char) <= 0x7E for char in password)):
        if not _HEX_PSK.fullmatch(password):
            _fail("wifi.password", "must be 8-63 printable ASCII characters or a 64-character hexadecimal key")
    return WifiSettings(country=country, ssid=ssid, password=password)


def _authorized_key(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if len(value.encode("utf-8")) > MAX_AUTHORIZED_KEY_BYTES:
        _fail("ssh.authorized_key", "is too long")
    # Options are intentionally unsupported.  A first-boot consumer can then
    # write this value as one complete authorized_keys line without interpreting
    # an option list supplied on removable media.
    parts = value.split(" ")
    if not parts or any(part == "" for part in parts[:2]) or len(parts) < 2:
        _fail("ssh.authorized_key", "must be one OpenSSH public key")
    key_type, encoded = parts[0], parts[1]
    if key_type not in _SSH_KEY_TYPES:
        _fail("ssh.authorized_key", "uses an unsupported public-key type")
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        _fail("ssh.authorized_key", "has invalid public-key encoding")
    if len(blob) < 32:
        _fail("ssh.authorized_key", "has an invalid public-key payload")
    if len(parts) > 2 and any(not part for part in parts[2:]):
        _fail("ssh.authorized_key", "has invalid spacing")
    return value


def _password(table: dict[str, Any], field_name: str, maximum: int) -> str | None:
    value = _text(table, field_name, required=False, maximum=maximum)
    if value is None or value == "":
        return None
    if not 8 <= len(value.encode("utf-8")) <= maximum:
        _fail(field_name, "has an invalid length")
    return value


def parse_settings(document: str | bytes) -> InstallerSettings:
    """Parse and validate an installer settings TOML document."""

    if isinstance(document, bytes):
        if len(document) > MAX_DOCUMENT_BYTES:
            raise SettingsError("installer settings document is too large")
        try:
            document = document.decode("utf-8")
        except UnicodeDecodeError:
            raise SettingsError("installer settings document is not valid UTF-8") from None
    elif not isinstance(document, str):
        raise SettingsError("installer settings document must be text")
    try:
        document_bytes = document.encode("utf-8")
    except UnicodeEncodeError:
        raise SettingsError("installer settings document is not valid UTF-8") from None
    if len(document_bytes) > MAX_DOCUMENT_BYTES:
        raise SettingsError("installer settings document is too large")
    try:
        parsed = tomllib.loads(document)
    except tomllib.TOMLDecodeError:
        raise SettingsError("installer settings document is not valid TOML") from None
    if not isinstance(parsed, dict):
        raise SettingsError("installer settings document must contain a table")
    if set(parsed) - _ROOT_KEYS:
        raise SettingsError("installer settings document contains an unknown key")

    installer = _table(parsed, "installer", _INSTALLER_KEYS, required=True)
    hostname = _hostname(_text(installer, "hostname", maximum=MAX_HOSTNAME_BYTES))
    username = _username(_text(installer, "username", maximum=MAX_USERNAME_BYTES))

    wifi = _wifi(_table(parsed, "wifi", _WIFI_KEYS, required=False))

    ssh_table = _table(parsed, "ssh", _SSH_KEYS, required=True)
    authorized_key = _authorized_key(_text(ssh_table, "authorized_key", required=False, maximum=MAX_AUTHORIZED_KEY_BYTES))
    login_password = _password(ssh_table, "password", MAX_LOGIN_PASSWORD_BYTES)
    if authorized_key is None and login_password is None:
        _fail("ssh", "must provide an authorized key or login password")

    rdp_table = _table(parsed, "rdp", _RDP_KEYS, required=True)
    rdp_password = _password(rdp_table, "password", MAX_RDP_PASSWORD_BYTES)
    if rdp_password is None:
        _fail("rdp.password", "is required")

    return InstallerSettings(
        installer=InstallerIdentity(hostname=hostname, username=username),
        wifi=wifi,
        ssh=SSHSettings(authorized_key=authorized_key, password=login_password),
        rdp=RdpSettings(password=rdp_password),
    )


def load_settings(path: str | Path) -> InstallerSettings:
    """Read one settings file and parse it without exposing its contents."""

    try:
        document = Path(path).read_bytes()
    except OSError:
        raise SettingsError("cannot read installer settings file") from None
    return parse_settings(document)


__all__ = [
    "InstallerIdentity",
    "InstallerSettings",
    "MAX_DOCUMENT_BYTES",
    "RdpSettings",
    "SSHSettings",
    "SettingsError",
    "WifiSettings",
    "load_settings",
    "parse_settings",
]
