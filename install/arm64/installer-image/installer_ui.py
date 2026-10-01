#!/usr/bin/python3
"""Small, readable terminal front end for the Raspberry Pi installer.

The controller remains the authority for discovery, validation, planning, and
the worker.  This module only collects answers and renders those responses.  A
``call`` callback is used deliberately so the same UI can be exercised with a
fake controller in tests and with the fixed privileged controller in the
installer image.
"""

from __future__ import annotations

import curses
import datetime
import json
import sys
import textwrap
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any


POLL_SECONDS = 5.0
ACTIVE_STATES = {"queued", "running"}
RESTART_CONFIRMATION = "RESTART FROM SCRATCH"
REPAIR_CONFIRMATION = "REPAIR BOOT ONLY"
MIN_HEIGHT = 15
MIN_WIDTH = 58

_BACK = object()
_CANCEL = object()


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _redact(value: Any, secrets: Sequence[str] = ()) -> str:
    result = _text(value)
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[hidden]")
    return result


def _human_size(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "size unknown"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return "size unknown"


def _disk_label(disk: Mapping[str, Any], *, unavailable: bool = False) -> str:
    path = _text(disk.get("path") or disk.get("name") or "unknown disk")
    size = _human_size(disk.get("size"))
    identity = disk.get("identity") if isinstance(disk.get("identity"), Mapping) else {}
    model = _text(disk.get("model") or identity.get("model") or disk.get("transport") or identity.get("transport") or "disk")
    suffix = ""
    reasons = disk.get("reasons")
    if unavailable:
        reason = ", ".join(_text(item) for item in reasons) if isinstance(reasons, list) and reasons else "not eligible for this role"
        suffix = " — unavailable: " + reason
    return f"{path}  {size}  {model}{suffix}"


def _disk_options(disks: Sequence[Mapping[str, Any]], *, key: bool = False) -> tuple[list[str], list[Mapping[str, Any]], list[str]]:
    available: list[Mapping[str, Any]] = []
    unavailable: list[str] = []
    for disk in disks:
        eligible_field = "key_eligible" if key else "eligible"
        eligible = disk.get(eligible_field)
        # An omitted eligibility bit is intentionally not a safe default.  The
        # controller owns the protected-media policy and must positively mark
        # a disk before it can appear as a selectable target.
        if eligible is True:
            available.append(disk)
        else:
            unavailable.append(_disk_label(disk, unavailable=True))
    return ([_disk_label(disk) for disk in available], available, unavailable)


def _phase_label(phase: Any, *, recovery: bool = False) -> str:
    phase = _text(phase).lower().replace("_", "-")
    if recovery or phase.startswith("recovery"):
        labels = {
            "recovery-queued": "Queue recovery",
            "recovery-repair": "Repair boot and initramfs",
            "recovery-complete": "Finish recovery",
        }
        return labels.get(phase, "Recover target")
    labels = {
        "idle": "Ready",
        "queued": "Waiting for the installer to start",
        "payload-validation": "Verify the desktop payload",
        "network-preflight": "Check required network access",
        "target-preparation": "Prepare target storage",
        "payload-copy": "Copy the desktop payload",
        "desktop-provisioning": "Configure the installed desktop",
        "finalizing": "Finalize the installation",
        "complete": "Installation complete",
    }
    return labels.get(phase, "Work on the installed target")


def _inspection_lines(facts: Any) -> list[str]:
    if not isinstance(facts, Mapping):
        return ["No inspection facts were returned."]
    lines = ["Inspection finished without changing the target."]
    for key, label in (("cmdline_present", "Boot command line"), ("config_present", "Boot configuration"), ("crypttab_present", "Encryption configuration"), ("provenance_present", "Installation receipt")):
        lines.append(f"{label}: {'present' if facts.get(key) else 'absent'}")
    if isinstance(facts.get("boot_files"), list):
        lines.append("Boot files: " + ", ".join(_text(item) for item in facts["boot_files"][:40]))
    return lines


def _merge_defaults(defaults: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy default answers without retaining a reference to the controller."""

    if not isinstance(defaults, Mapping):
        return {}
    try:
        return json.loads(json.dumps(defaults))
    except (TypeError, ValueError):
        return {key: value for key, value in defaults.items()}


class CursesInteraction:
    """The deliberately small curses adapter used by :class:`InstallerUi`."""

    def __init__(self, screen: Any) -> None:
        self.screen = screen
        try:
            self.screen.keypad(True)
            curses.curs_set(0)
        except (AttributeError, curses.error):
            pass
        self._colors = False
        try:
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_CYAN, -1)
                curses.init_pair(2, curses.COLOR_GREEN, -1)
                curses.init_pair(3, curses.COLOR_RED, -1)
                self._colors = True
        except (AttributeError, curses.error):
            pass

    def _size(self) -> tuple[int, int]:
        return self.screen.getmaxyx()

    def _put(self, row: int, col: int, value: str, *, attr: int = 0) -> None:
        height, width = self._size()
        if row < 0 or row >= height or col >= width:
            return
        value = _text(value)
        try:
            self.screen.addnstr(row, max(0, col), value, max(0, width - col - 1), attr)
        except (AttributeError, curses.error):
            pass

    def _draw(self, title: str, lines: Sequence[str], *, selected: int | None = None, footer: str = "Enter select   ↑↓ move   Esc back") -> None:
        height, width = self._size()
        self.screen.erase()
        if height < MIN_HEIGHT or width < MIN_WIDTH:
            self._put(max(0, height // 2 - 1), 2, f"Please resize the terminal to at least {MIN_WIDTH}x{MIN_HEIGHT}.", attr=self._attr(3))
            self._put(max(0, height // 2 + 1), 2, f"Current size: {width}x{height}")
            self._put(height - 1, 2, "Esc or q to leave")
            self.screen.refresh()
            return
        self._put(1, 3, "OMARCHY PI  /  INSTALLER", attr=self._attr(1))
        self._put(2, 3, title, attr=self._attr(1))
        self._put(3, 3, "─" * max(1, width - 6))
        usable = max(1, width - 8)
        row = 5
        for index, original in enumerate(lines):
            wrapped = textwrap.wrap(_text(original), usable) or [""]
            for part_index, part in enumerate(wrapped):
                marker = "› " if selected == index and part_index == 0 else "  "
                attr = self._attr(2) if selected == index else 0
                self._put(row, 3, marker + part, attr=attr)
                row += 1
                if row >= height - 2:
                    break
            if row >= height - 2:
                break
        self._put(height - 2, 3, "─" * max(1, width - 6))
        self._put(height - 1, 3, footer)
        self.screen.refresh()

    def _attr(self, color: int) -> int:
        if not self._colors:
            return 0
        try:
            return curses.color_pair(color)
        except (AttributeError, curses.error):
            return 0

    def _key(self) -> int | str:
        try:
            value = self.screen.get_wch()
        except (AttributeError, curses.error):
            return 27
        if isinstance(value, str) and ord(value) >= 256:
            return value
        value = ord(value) if isinstance(value, str) else value
        return 10 if value == getattr(curses, "KEY_ENTER", -999) else value

    def choose(self, title: str, options: Sequence[str], *, detail: Sequence[str] = (), selected: int = 0) -> int | object | None:
        if not options:
            return _BACK
        selected = min(max(0, selected), len(options) - 1)
        while True:
            height, width = self._size()
            visible = max(1, height - 10)
            top = max(0, min(selected, max(0, len(options) - visible)))
            # Keep every selectable row on one line; explanatory text follows.
            rows = [line if len(line) <= width - 9 else line[:max(1, width - 10)] + "…" for line in options[top:top + visible]]
            self._draw(title, rows + [""] + list(detail), selected=selected - top)
            key = self._key()
            if key in (27, ord("q")):
                return _BACK
            if height < MIN_HEIGHT or width < MIN_WIDTH:
                continue
            if key in (curses.KEY_UP, ord("k")):
                selected = (selected - 1) % len(options)
            elif key in (curses.KEY_DOWN, ord("j"), 9):
                selected = (selected + 1) % len(options)
            elif key in (10, 13):
                return selected

    def text(self, label: str, initial: str = "", *, secret: bool = False, required: bool = False) -> str | object:
        value = list(_text(initial))
        cursor = len(value)
        while True:
            height, width = self._size()
            field_width = max(1, width - 10)
            left = max(0, cursor - field_width + 1)
            shown = "".join("•" for _ in value) if secret else "".join(value)
            self._draw("Edit answer", [shown[left:left + field_width] or " ", "", label], footer="Enter save   Esc back   Ctrl-U clear")
            try:
                curses.curs_set(1)
                self.screen.move(5, min(width - 2, 5 + cursor - left))
            except curses.error:
                pass
            key = self._key()
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            if key == 27:
                return _BACK
            if height < MIN_HEIGHT or width < MIN_WIDTH:
                continue
            if key in (10, 13):
                answer = "".join(value)
                if required and not answer.strip():
                    self.message("Answer needed", [label, "Please enter a value."])
                    continue
                return answer
            if key == curses.KEY_LEFT:
                cursor = max(0, cursor - 1)
            elif key == curses.KEY_RIGHT:
                cursor = min(len(value), cursor + 1)
            elif key == curses.KEY_HOME:
                cursor = 0
            elif key == curses.KEY_END:
                cursor = len(value)
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                if cursor:
                    del value[cursor - 1]
                    cursor -= 1
            elif key == getattr(curses, "KEY_DC", -1000):
                if cursor < len(value):
                    del value[cursor]
            elif key == 21:
                value, cursor = [], 0
            elif isinstance(key, str) and key.isprintable():
                value.insert(cursor, key)
                cursor += 1
            elif isinstance(key, int) and 32 <= key <= 255:
                value.insert(cursor, chr(key))
                cursor += 1

    def confirm(self, question: str) -> bool:
        result = self.choose(question, ["Yes", "No"], detail=["Choose Yes to continue."])
        return result == 0

    def message(self, title: str, lines: Sequence[str]) -> None:
        self.message_with_review(title, lines, review=False)

    def message_with_review(self, title: str, lines: Sequence[str], *, review: bool = True) -> bool:
        top = 0
        while True:
            height, width = self._size()
            wrapped = [part for line in lines for part in (textwrap.wrap(_text(line), max(1, width - 8)) or [""])]
            visible = max(1, height - 7)
            top = min(top, max(0, len(wrapped) - visible))
            footer = "Enter continue   Esc back" if review else "Enter or Esc to return"
            if len(wrapped) > visible:
                footer += "   ↑↓ scroll"
            self._draw(title, wrapped[top:top + visible], footer=footer)
            key = self._key()
            if key in (27, ord("q")):
                return False
            if height < MIN_HEIGHT or width < MIN_WIDTH:
                continue
            if key in (10, 13):
                return True
            if key in (curses.KEY_DOWN, ord("j")):
                top = min(top + 1, max(0, len(wrapped) - visible))
            elif key in (curses.KEY_UP, ord("k")):
                top = max(0, top - 1)
            elif key == curses.KEY_NPAGE:
                top = min(top + visible, max(0, len(wrapped) - visible))
            elif key == curses.KEY_PPAGE:
                top = max(0, top - visible)

    def watch(
        self,
        state: Mapping[str, Any],
        call: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        sleep_fn: Callable[[float], None],
        *,
        secrets: Sequence[str] = (),
    ) -> int | None:
        current = dict(state)
        started = time.time()
        try:
            started = datetime.datetime.fromisoformat(_text(current.get("started_at"))).timestamp()
        except ValueError:
            pass
        recovery = _text(current.get("kind")).lower() == "recovery" or _text(current.get("phase")).startswith("recovery")
        status_error: Exception | None = None
        self.screen.nodelay(True)
        try:
            while current.get("status") in ACTIVE_STATES:
                elapsed = max(0, int(time.time() - started))
                minutes, seconds = divmod(elapsed, 60)
                activity = ("·   ", "··  ", "··· ", "····")[(elapsed // 2) % 4]
                lines = [
                    _phase_label(current.get("phase"), recovery=recovery),
                    f"Working {activity}    Elapsed {minutes:02d}:{seconds:02d}",
                    _redact(current.get("message")), "",
                    "You can disconnect and return; the job keeps running.",
                    "Reopen omarchy-pi-install to see its progress.",
                ]
                self._draw("Recovery in progress" if recovery else "Installation in progress", lines, footer="q/Esc leave progress view")
                if self.screen.getch() in (27, ord("q")):
                    return None
                sleep_fn(POLL_SECONDS)
                try:
                    response = call({"action": "status"})
                    if not isinstance(response, Mapping):
                        raise RuntimeError("installer service returned invalid status data")
                    current = dict(response.get("state", {}))
                except Exception as exc:
                    status_error = exc
                    break
                recovery = recovery or _text(current.get("kind")).lower() == "recovery"
        finally:
            # Error screens must accept input even if status polling fails.
            self.screen.nodelay(False)
        if status_error is not None:
            self.message("Progress unavailable", [
                _redact(str(status_error), secrets),
                "The job was accepted and may still be running in the background.",
                "Return to Installer home and choose View latest job before retrying.",
            ])
            return None
        if current.get("status") == "complete":
            self.completion(current)
            self.after_completion(current, call)
            return 0
        self.message("Worker stopped", [
            "Stopped while: " + _phase_label(current.get("phase"), recovery=recovery),
            _redact(current.get("error") or "The worker did not complete."),
            "Open View latest job to review the result.",
            "Diagnostic log: " + _text(current.get("log_file") or "not available"),
        ])
        return None

    def completion(self, state: Mapping[str, Any]) -> None:
        summary = state.get("summary")
        recovery = _text(state.get("kind")).lower() == "recovery"
        lines = ["Boot repair finished." if recovery else "Your Omarchy desktop is ready to boot.", ""]
        if isinstance(summary, Mapping) and not recovery:
            hostname = _text(summary.get("hostname") or "your Pi")
            username = _text(summary.get("username") or "<user>")
            lines.append(f"Computer: {hostname}    Account: {username}")
            if summary.get("ssh_enabled") or summary.get("rdp_mode") in {"lan", "loopback"}:
                lines.append("After boot, find the Pi's address in your router's device list.")
            if summary.get("ssh_enabled"):
                lines.append(f"SSH: ssh {username}@<Pi-address>")
                if summary.get("ssh_host_key_fingerprint"):
                    lines.append("SSH fingerprint: " + _text(summary["ssh_host_key_fingerprint"]))
            if summary.get("rdp_mode") == "loopback":
                lines.extend([f"RDP tunnel: ssh -L 3389:127.0.0.1:3389 {username}@<Pi-address>", "Then connect your RDP client to 127.0.0.1:3389."])
            elif summary.get("rdp_mode") == "lan":
                lines.append("Remote desktop: connect your RDP client to <Pi-address>:3389.")
            if summary.get("encryption") == "key":
                lines.append("Keep the unlock-key USB connected when booting the Pi.")
            elif summary.get("encryption") == "passphrase":
                lines.append("Enter your recovery passphrase at the Pi's boot prompt.")
        lines.extend(["", "1. Shut down the installer.", "2. Remove the installer USB; leave the target connected.", "3. Power on the Pi to start Omarchy."])
        self.message("Recovery complete" if recovery else "Installation complete", lines)

    def after_completion(self, state: Mapping[str, Any], call: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> None:
        """Offer the safe next action after the completion card is read."""

        del state
        choice = self.choose("Next", ["Close installer view", "Shut down installer"])
        if choice != 1:
            return
        phrase = self.text("Type SHUT DOWN INSTALLER to power off the installer", required=True)
        if phrase != "SHUT DOWN INSTALLER":
            self.message("Shutdown cancelled", ["The installer remains running."])
            return
        try:
            call({"action": "shutdown", "confirmation": "SHUT DOWN INSTALLER"})
            self.message("Shutdown requested", ["The installer is shutting down. Remove the installer USB after power-off."])
        except Exception as exc:
            self.message("Shutdown unavailable", [_redact(str(exc)), "Leave the installer running and use View latest job if needed."])


class InstallerUi:
    """Installer workflow, independent of the terminal renderer."""

    SECTION_LABELS = [
        "Account & regional",
        "Network",
        "Remote access",
        "Encryption",
        "Review and continue",
        "Back",
    ]
    def __init__(
        self,
        call: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        validate_settings: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        interaction: Any,
        defaults: Mapping[str, Any] | None = None,
    ) -> None:
        self.call = call
        self.validate_settings = validate_settings
        self.ui = interaction
        self.defaults = _merge_defaults(defaults)

    def _call(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        response = self.call(dict(request))
        if not isinstance(response, Mapping):
            raise RuntimeError("installer service returned invalid data")
        if response.get("ok") is False:
            raise RuntimeError(_redact(response.get("error") or "installer service rejected the request"))
        return response

    def _error(self, title: str, exc: Exception, *, secrets: Sequence[str] = (), phase: str = "setup") -> None:
        if phase == "submit":
            follow_up = "No accepted response was received; the job may already be running. No retry was attempted. Choose View latest job before starting another install."
        elif phase == "status":
            follow_up = "The job was accepted and may still be running in the background. Progress could not be read. Choose View latest job before retrying."
        else:
            follow_up = "No new install or repair was submitted by this check."
        self.ui.message(title, [_redact(str(exc), secrets), follow_up])

    def _state(self) -> Mapping[str, Any]:
        return self._call({"action": "status"}).get("state", {})

    def _startup_state(self) -> Mapping[str, Any] | None:
        while True:
            try:
                return self._state()
            except Exception as exc:
                choice = self.ui.choose(
                    "Installer status unavailable",
                    ["Retry status", "Exit installer"],
                    detail=[_redact(str(exc)), "No new install or repair was submitted by this check."],
                )
                if choice != 0:
                    return None

    def run(self) -> int:
        try:
            state = self._startup_state()
        except (EOFError, KeyboardInterrupt):
            self.ui.message("Installer view closed", ["No new install or repair was submitted by this check."])
            return 1
        except Exception as exc:
            self._error("Installer unavailable", exc)
            return 0
        if state is None:
            return 0
        if _text(state.get("status")) in ACTIVE_STATES:
            result = self.ui.watch(state, self._call, time.sleep)
            if result is not None:
                return result
        while True:
            try:
                choice = self.ui.choose(
                    "Installer home",
                    ["Install Omarchy on a target", "Repair target boot files", "View latest job", "Exit"],
                    detail=["Install a new Omarchy desktop, or repair an existing installation."],
                )
                if choice in (_BACK, _CANCEL, 3, None):
                    return 0
                if choice == 0:
                    # The state may have changed while the user read the home
                    # screen, so never use the initial snapshot for restart.
                    state = self._state()
                    if _text(state.get("status")) in ACTIVE_STATES:
                        result = self.ui.watch(state, self._call, time.sleep)
                    else:
                        result = self.install(state)
                    if result is not None:
                        return result
                elif choice == 1:
                    result = self.recover()
                    if result is not None:
                        return result
                elif choice == 2:
                    result = self.latest_job()
                    if result is not None:
                        return result
            except (EOFError, KeyboardInterrupt):
                self.ui.message("Installer view closed", ["Any submitted job keeps running. Reopen the installer to check its status."])
                return 1
            except Exception as exc:
                self._error("Installer unavailable", exc)

    def latest_job(self) -> int | None:
        try:
            state = self._state()
        except Exception as exc:
            self._error("Could not read latest job", exc)
            return None
        if _text(state.get("status")) in ACTIVE_STATES:
            return self.ui.watch(state, self._call, time.sleep)
        if _text(state.get("status")) == "complete":
            self.ui.completion(state)
            return None
        lines = [f"Status: {_text(state.get('status') or 'idle')}", f"Phase: {_phase_label(state.get('phase'))}"]
        if state.get("error"):
            lines.append("Reason: " + _redact(state.get("error")))
        self.ui.message("Latest job", lines)
        return None

    def _reuse_defaults(self) -> dict[str, Any] | object:
        if not self.defaults:
            return {}
        choice = self.ui.choose(
            "Saved installer answers found",
            ["Start fresh", "Reuse saved answers", "Cancel"],
            detail=["Saved answers are held only for this installer session; secrets stay masked."],
        )
        if choice == 1:
            return _merge_defaults(self.defaults)
        if choice in (_BACK, _CANCEL, 2, None):
            return _CANCEL
        return {}

    def _base_settings(self, initial: Mapping[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {
            "username": "",
            "hostname": "",
            "password": "",
            "timezone": "Europe/London",
            "locale": "en_GB.UTF-8",
            "keymap": "us",
            "wifi": None,
            "ssh_enabled": False,
            "ssh_authorized_key": None,
            "rdp_mode": "disabled",
            "rdp_password": None,
            "encryption": "plain",
            "recovery_passphrase": None,
        }
        for key, value in initial.items():
            if key not in values:
                continue
            values[key] = _merge_defaults(value) if isinstance(value, Mapping) else value
        if not isinstance(values.get("wifi"), Mapping):
            values["wifi"] = None
        return values

    def _choose_value(
        self,
        title: str,
        options: Sequence[tuple[str, Any]],
        current: Any,
        *,
        detail: Sequence[str] = (),
    ) -> Any:
        values = [value for _label, value in options]
        current_index = values.index(current) if current in values else 0
        selected = self.ui.choose(title, [label for label, _value in options], detail=detail, selected=current_index)
        if selected in (_BACK, _CANCEL) or not isinstance(selected, int):
            return _BACK
        return values[selected]

    def _ask_secret_pair(
        self,
        label: str,
        current: str | None = None,
        *,
        reuse_question: str | None = None,
    ) -> str | object:
        current = current if isinstance(current, str) else ""
        if current and reuse_question and not self.ui.confirm(reuse_question):
            current = ""
        while True:
            answer = self.ui.text(
                label + (" (Enter keeps saved answer)" if current else ""),
                current,
                secret=True,
                required=True,
            )
            if answer in (_BACK, _CANCEL):
                return answer
            if answer == current and current:
                return answer
            confirm = self.ui.text("Confirm " + label.lower(), "", secret=True, required=True)
            if confirm in (_BACK, _CANCEL):
                return confirm
            if answer == confirm:
                return answer
            self.ui.message("Answers do not match", ["Enter the new value twice. Other sections remain unchanged."])
            current = ""

    def _edit_account(self, values: dict[str, Any]) -> None:
        fields = (
            ("username", "Target account name", True),
            ("hostname", "Target hostname", True),
            ("timezone", "Timezone", True),
            ("locale", "Locale", True),
            ("keymap", "Keyboard layout", True),
        )
        for name, label, required in fields:
            result = self.ui.text(label, _text(values.get(name)), required=required)
            if result in (_BACK, _CANCEL):
                return
            values[name] = result
        password = self._ask_secret_pair("Target account password", _text(values.get("password")) or None)
        if password not in (_BACK, _CANCEL):
            values["password"] = password

    def _edit_network(self, values: dict[str, Any]) -> None:
        old_wifi = values.get("wifi") if isinstance(values.get("wifi"), Mapping) else None
        wifi = self._choose_value(
            "Wi-Fi",
            [("Do not configure Wi-Fi", False), ("Configure Wi-Fi", True)],
            bool(old_wifi),
            detail=["Wired networking or later setup remains available."],
        )
        if wifi is _BACK:
            return
        if not wifi:
            values["wifi"] = None
            return
        current_wifi = _merge_defaults(old_wifi)
        values["wifi"] = current_wifi
        for name, label in (("country", "Wi-Fi country"), ("ssid", "Wi-Fi network name")):
            result = self.ui.text(label, _text(current_wifi.get(name)), required=True)
            if result in (_BACK, _CANCEL):
                return
            current_wifi[name] = result
        password = self._ask_secret_pair(
            "Wi-Fi password",
            _text(current_wifi.pop("password", None)) or None,
            reuse_question="Reuse the saved Wi-Fi password?",
        )
        if password not in (_BACK, _CANCEL):
            current_wifi["password"] = password

    def _edit_remote(self, values: dict[str, Any]) -> None:
        ssh = self._choose_value(
            "SSH access",
            [("Disable SSH", False), ("Enable SSH", True)],
            bool(values.get("ssh_enabled")),
            detail=["SSH is useful for headless Pi setup and recovery."],
        )
        if ssh is _BACK:
            return
        values["ssh_enabled"] = ssh
        if not ssh:
            values["ssh_authorized_key"] = None
        else:
            key = self.ui.text("Authorized SSH public key (blank to omit)", _text(values.get("ssh_authorized_key")), required=False)
            if key not in (_BACK, _CANCEL):
                values["ssh_authorized_key"] = key or None

        rdp = self._choose_value(
            "Remote desktop",
            [("Disabled", "disabled"), ("Loopback (SSH tunnel)", "loopback"), ("LAN (direct client)", "lan")],
            values.get("rdp_mode", "disabled"),
            detail=["Loopback suits headless access; LAN exposes RDP on the local network."],
        )
        if rdp is _BACK:
            return
        values["rdp_mode"] = rdp
        if rdp == "disabled":
            values["rdp_password"] = None
            return
        password = self._ask_secret_pair(
            "RDP password",
            _text(values.pop("rdp_password", None)) or None,
            reuse_question="Reuse the saved RDP password?",
        )
        if password not in (_BACK, _CANCEL):
            values["rdp_password"] = password

    def _edit_encryption(self, values: dict[str, Any]) -> None:
        encryption = self._choose_value(
            "Target storage",
            [
                ("No disk encryption", "plain"),
                ("Encrypted: passphrase at boot", "passphrase"),
                ("Encrypted: separate USB key", "key"),
            ],
            values.get("encryption", "plain"),
            detail=[
                "Passphrase encryption needs a local keyboard at boot before SSH/RDP is available.",
                "The separate key USB is erased during setup and must stay inserted to boot.",
            ],
        )
        if encryption is _BACK:
            return
        values["encryption"] = encryption
        if encryption == "plain":
            values["recovery_passphrase"] = None
            return
        recovery = self._ask_secret_pair("Recovery passphrase", _text(values.get("recovery_passphrase")) or None)
        if recovery not in (_BACK, _CANCEL):
            values["recovery_passphrase"] = recovery

    def _collect_settings(self, initial: Mapping[str, Any]) -> dict[str, Any] | object:
        values = self._base_settings(initial)
        selected_section = 0
        while True:
            choice = self.ui.choose(
                "Installation settings",
                self.SECTION_LABELS,
                detail=["Edit a section; your answers stay in place when you go back."],
                selected=selected_section,
            )
            if choice in (_BACK, _CANCEL, 5, None):
                return _BACK
            if not isinstance(choice, int):
                return _BACK
            selected_section = choice
            if choice == 0:
                self._edit_account(values)
            elif choice == 1:
                self._edit_network(values)
            elif choice == 2:
                self._edit_remote(values)
            elif choice == 3:
                self._edit_encryption(values)
            elif choice == 4:
                try:
                    validated = self.validate_settings(values)
                    if not isinstance(validated, Mapping):
                        raise ValueError("settings validator returned an invalid result")
                    return dict(validated)
                except Exception as exc:
                    hidden = [
                        _text(values.get("password")),
                        _text(values.get("rdp_password")),
                        _text(values.get("recovery_passphrase")),
                    ]
                    wifi = values.get("wifi")
                    if isinstance(wifi, Mapping):
                        hidden.append(_text(wifi.get("password")))
                    self.ui.message("Review settings", [_redact(str(exc), hidden), "Choose a section to correct; your answers remain in place."])

    def _select_disk(self, disks: Sequence[Mapping[str, Any]], *, key: bool = False, title: str | None = None) -> Mapping[str, Any] | object:
        options, available, unavailable = _disk_options(disks, key=key)
        if not available:
            self.ui.message("No eligible disk", unavailable or ["The installer found no eligible disk for this role."])
            return _CANCEL
        selected = self.ui.choose(
            title or ("Select target USB/NVMe" if not key else "Select disposable key USB"),
            options,
            detail=["Unavailable disks are listed with their reason and cannot be selected."] + unavailable,
        )
        if selected in (_BACK, _CANCEL) or not isinstance(selected, int):
            return _BACK
        return available[selected]

    @staticmethod
    def _recovery_key_label(key: Mapping[str, Any]) -> str:
        path = _text(key.get("path") or key.get("name") or "unknown key partition")
        size = _human_size(key.get("size"))
        model = _text(key.get("model") or "unlock-key USB")
        stable_id = _text(key.get("stable_id"))
        uuid = _text(key.get("uuid"))
        identity: list[str] = []
        if stable_id:
            identity.append("parent=" + stable_id)
        if uuid:
            identity.append("UUID=" + uuid)
        fields = [path] + identity + [size, model]
        return "  ".join(field for field in fields if field)

    @classmethod
    def _recovery_key_details(cls, key: Mapping[str, Any]) -> list[str]:
        details = [
            "Partition: " + _text(key.get("path") or key.get("name") or "unknown"),
            "Parent stable ID: " + _text(key.get("stable_id") or "unknown"),
            "Filesystem UUID: " + _text(key.get("uuid") or "unknown"),
            "Size: " + _human_size(key.get("size")),
            "Model: " + _text(key.get("model") or "unknown"),
        ]
        if key.get("transport"):
            details.append("Transport: " + _text(key.get("transport")))
        return details

    def _select_recovery_key(self, keys: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | object:
        available = [key for key in keys if key.get("eligible") is True]
        unavailable: list[str] = []
        for key in keys:
            if key.get("eligible") is True:
                continue
            reasons = key.get("reasons")
            if not isinstance(reasons, Sequence) or isinstance(reasons, (str, bytes)):
                reasons = ["not eligible for this role"]
            unavailable.append(self._recovery_key_label(key) + " — unavailable: " + ", ".join(_text(item) for item in reasons))
        if not available:
            self.ui.message("No eligible unlock-key USB", unavailable or ["The installer found no eligible attached unlock-key USB."])
            return _CANCEL
        selected = self.ui.choose(
            "Select attached unlock-key USB",
            [self._recovery_key_label(key) for key in available],
            detail=[
                "Select the key partition whose parent USB and filesystem UUID match the key you intend to use.",
                "The installer controller mounts the selected key temporarily read-only; it does not format it.",
            ] + unavailable,
        )
        if selected in (_BACK, _CANCEL) or not isinstance(selected, int):
            return _BACK
        key = available[selected]
        if not self.ui.message_with_review("Selected attached unlock-key USB", self._recovery_key_details(key)):
            return _CANCEL
        return key

    @staticmethod
    def _token(plan: Mapping[str, Any], name: str) -> str:
        direct = plan.get(name + "_token")
        if direct is not None:
            return _text(direct)
        value = plan.get(name)
        if isinstance(value, Mapping):
            return _text(value.get("token"))
        return ""

    def _plan_lines(self, plan: Mapping[str, Any], target: Mapping[str, Any], key: Mapping[str, Any] | None, settings: Mapping[str, Any] | None = None) -> list[str]:
        lines = ["The installer accepted the answers and produced this plan:", "", f"Target storage: {_disk_label(target)}"]
        if key is not None:
            lines.append(f"Disposable key: {_disk_label(key)}")
        if isinstance(settings, Mapping):
            lines.extend(["", "Settings:"])
            rdp_labels = {"disabled": "Disabled", "loopback": "Loopback (SSH tunnel)", "lan": "LAN (direct client)"}
            encryption_labels = {
                "plain": "No disk encryption",
                "passphrase": "Encrypted: passphrase at boot",
                "key": "Encrypted: separate USB key",
            }
            for name, label in (("username", "Account"), ("hostname", "Hostname"), ("timezone", "Timezone"), ("locale", "Locale"), ("keymap", "Keyboard")):
                lines.append(f"  {label}: {_text(settings.get(name))}")
            lines.extend([
                f"  Wi-Fi: {'configured' if settings.get('wifi') else 'not configured'}",
                f"  SSH: {'enabled' if settings.get('ssh_enabled') else 'disabled'}",
                f"  Remote desktop: {rdp_labels.get(_text(settings.get('rdp_mode')), 'Disabled')}",
                f"  Encryption: {encryption_labels.get(_text(settings.get('encryption')), 'No disk encryption')}",
            ])
        payload = plan.get("payload")
        if isinstance(payload, Mapping):
            if payload.get("required_target_bytes") is not None:
                lines.append("Desktop payload size: " + _human_size(payload.get("required_target_bytes")))
            if payload.get("source_revision"):
                lines.append("Desktop payload revision: " + _text(payload.get("source_revision")))
        installer = plan.get("installer")
        if isinstance(installer, Mapping):
            revision = installer.get("revision") or installer.get("source_revision") or installer.get("commit")
            if revision:
                lines.append("Installer revision: " + _text(revision))
        lines.extend(["", "The target token below is an exact erase safeguard.", "Submitting the exact erase token and internet consent starts installation."])
        return lines

    def install(self, previous_state: Mapping[str, Any]) -> int | None:
        restart = _text(previous_state.get("status")) in {"complete", "failed", "interrupted"}
        if restart:
            phrase = self.ui.text("Type RESTART FROM SCRATCH to begin a new install", required=True)
            if phrase != RESTART_CONFIRMATION:
                self.ui.message("Install cancelled", ["The recorded job remains available."])
                return None
        initial = self._reuse_defaults()
        if initial is _CANCEL:
            return None
        secrets: list[str] = []
        submission_attempted = False
        accepted = False
        try:
            disks = self._call({"action": "discover"}).get("disks", [])
            target = self._select_disk(disks)
            if target in (_BACK, _CANCEL):
                return None
            settings = self._collect_settings(initial)
            if settings in (_BACK, _CANCEL):
                return None
            for name in ("password", "rdp_password", "recovery_passphrase"):
                if isinstance(settings.get(name), str):
                    secrets.append(settings[name])
            wifi = settings.get("wifi")
            if isinstance(wifi, Mapping) and isinstance(wifi.get("password"), str):
                secrets.append(wifi["password"])
            key: Mapping[str, Any] | None = None
            if settings.get("encryption") == "key":
                key = self._select_disk(disks, key=True)
                if key in (_BACK, _CANCEL):
                    return None
            request = {"action": "plan", "settings": settings, "target": target.get("path"), "key": key.get("path") if key else None}
            plan = self._call(request)
            if not self.ui.message_with_review("Review installation", self._plan_lines(plan, target, key, settings)):
                return None
            target_token = self._token(plan, "target")
            target_confirmation = self.ui.text("Type this target token exactly: " + target_token, required=True)
            if target_confirmation != target_token:
                self.ui.message("Target confirmation failed", ["No disk was changed."])
                return None
            key_confirmation = None
            key_token = self._token(plan, "key")
            if key_token:
                key_confirmation = self.ui.text("Type this key token exactly: " + key_token, required=True)
                if key_confirmation != key_token:
                    self.ui.message("Key confirmation failed", ["No disk was changed."])
                    return None
            consent = self.ui.text("The installer downloads required components. Type YES to consent to internet access", required=True)
            if consent != "YES":
                self.ui.message("Install cancelled", ["Internet consent was not given; no disk was changed."])
                return None
            submit: dict[str, Any] = {
                "action": "restart" if restart else "submit",
                "settings": settings,
                "target": target.get("path"),
                "key": key.get("path") if key else None,
                "target_confirmation": target_confirmation,
                "key_confirmation": key_confirmation,
                "consent_internet": True,
            }
            if restart:
                submit["restart_confirmation"] = RESTART_CONFIRMATION
            submission_attempted = True
            result = self._call(submit)
            accepted = True
            return self.ui.watch(result.get("state", {}), self._call, time.sleep, secrets=secrets)
        except Exception as exc:
            phase = "status" if accepted else "submit" if submission_attempted else "setup"
            self._error("Install could not continue", exc, secrets=secrets, phase=phase)
            return None

    def recover(self) -> int | None:
        secrets: list[str] = []
        submission_attempted = False
        accepted = False
        try:
            discovered = self._call({"action": "recovery-discover"})
            targets = discovered.get("targets", [])
            keys = discovered.get("keys", [])
            target = self._select_disk(targets, title="Select target to repair")
            if target in (_BACK, _CANCEL):
                return None
            token = _text(target.get("token"))
            confirmation = self.ui.text("Type this target token exactly: " + token, required=True)
            if confirmation != token:
                self.ui.message("Repair cancelled", ["Target confirmation failed."])
                return None
            encrypted = bool(target.get("luks_uuid"))
            request: dict[str, Any] = {"action": "recovery-plan", "target": target.get("path"), "target_confirmation": confirmation}
            if encrypted:
                credential_options = ["Use recovery passphrase"]
                if isinstance(keys, Sequence) and not isinstance(keys, (str, bytes)) and any(
                    isinstance(key, Mapping) and key.get("eligible") is True for key in keys
                ):
                    credential_options.append("Use attached unlock-key USB")
                credential_options.append("Use existing recovery key FILE")
                credential = self.ui.choose(
                    "Unlock encrypted target for inspection",
                    credential_options,
                    detail=["The selected credential is sent only to the installer service and stays masked."] ,
                )
                if credential in (_BACK, _CANCEL):
                    return None
                if credential == 0:
                    passphrase = self.ui.text("Recovery passphrase", secret=True, required=True)
                    if passphrase in (_BACK, _CANCEL):
                        return None
                    secrets.append(passphrase)
                    request["passphrase"] = passphrase
                elif credential == 1 and len(credential_options) == 3:
                    key = self._select_recovery_key(keys)
                    if key in (_BACK, _CANCEL):
                        return None
                    key_token = _text(key.get("token"))
                    if not key_token:
                        self.ui.message("Unlock-key confirmation unavailable", ["The selected key has no confirmation token. No target was changed."])
                        return None
                    key_confirmation = self.ui.text("Type this unlock-key token exactly: " + key_token, required=True)
                    if key_confirmation != key_token:
                        self.ui.message("Unlock-key confirmation failed", ["No target was changed."])
                        return None
                    request["key_device"] = key.get("path")
                    request["key_confirmation"] = key_confirmation
                else:
                    key_file = self.ui.text("Existing recovery key FILE path", required=True)
                    if key_file in (_BACK, _CANCEL):
                        return None
                    request["key_file"] = key_file
            plan = self._call(request)
            lines = ["Read-only plan", "Scope: " + _text(plan.get("scope") or "boot and initramfs only")]
            limitations = plan.get("limitations")
            if isinstance(limitations, list):
                lines.extend("Limit: " + _text(item) for item in limitations)
            if not self.ui.message_with_review("Repair review", lines):
                return None
            inspect = self._call({**request, "action": "recovery-inspect"})
            self.ui.message("Read-only inspection", _inspection_lines(inspect.get("inspection")))
            if not self.ui.confirm("Continue to boot repair?"):
                return None
            repair_request = {**request, "action": "recovery-repair", "repair_confirmation": REPAIR_CONFIRMATION}
            submission_attempted = True
            result = self._call(repair_request)
            accepted = True
            return self.ui.watch(result.get("state", {"status": "complete", "kind": "recovery", "summary": result}), self._call, time.sleep, secrets=secrets)
        except Exception as exc:
            phase = "status" if accepted else "submit" if submission_attempted else "setup"
            self._error("Recovery could not continue", exc, secrets=secrets, phase=phase)
            return None


def run(
    call: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    validate_settings: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    defaults: Mapping[str, Any] | None = None,
    *,
    interaction: Any | None = None,
) -> int:
    """Run the guided installer; tests can supply an interaction adapter."""

    if interaction is not None:
        return InstallerUi(call, validate_settings, interaction, defaults=defaults).run()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("The installer needs an interactive terminal. For SSH, use ssh -t with omarchy-pi-install.", file=sys.stderr)
        return 1
    try:
        return curses.wrapper(lambda screen: InstallerUi(call, validate_settings, CursesInteraction(screen), defaults=defaults).run())
    except curses.error:
        print("Could not open the installer terminal. Check your terminal's TERM setting and try again.", file=sys.stderr)
        return 1


__all__ = ["InstallerUi", "CursesInteraction", "run", "REPAIR_CONFIRMATION", "RESTART_CONFIRMATION"]
