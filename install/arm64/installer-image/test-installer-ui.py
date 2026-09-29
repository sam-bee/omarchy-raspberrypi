#!/usr/bin/env python3
"""Focused tests for the controller-facing installer UI workflow."""

from __future__ import annotations

import importlib.util
import io
import pathlib
import sys
import unittest
from unittest import mock
from collections.abc import Mapping, Sequence
from typing import Any


HERE = pathlib.Path(__file__).parent


def _load(name: str, path: pathlib.Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


installer_ui = _load("installer_ui_under_test", HERE / "installer_ui.py")
installed_target = _load("installed_target_under_test", HERE / "installed_target.py")


class FakeInteraction:
    def __init__(
        self,
        choices: Sequence[Any] = (),
        texts: Sequence[Any] = (),
        confirms: Sequence[bool] = (),
        reviews: Sequence[bool] = (),
    ) -> None:
        self.choices = list(choices)
        self.texts = list(texts)
        self.confirms = list(confirms)
        self.review_results = list(reviews)
        self.confirm_questions: list[str] = []
        self.messages: list[tuple[str, list[str]]] = []
        self.reviews: list[tuple[str, list[str]]] = []
        self.watched: list[Mapping[str, Any]] = []
        self.choose_calls: list[tuple[str, int]] = []
        self.completions: list[Mapping[str, Any]] = []

    def choose(self, _title: str, _options: Sequence[str], *, detail: Sequence[str] = (), selected: int = 0) -> Any:
        del detail
        self.choose_calls.append((_title, selected))
        if not self.choices:
            raise AssertionError(f"fake choice queue is empty for {_title!r}")
        return self.choices.pop(0)

    def text(self, _label: str, _initial: str = "", *, secret: bool = False, required: bool = False) -> Any:
        del secret, required
        if not self.texts:
            raise AssertionError(f"fake text queue is empty for {_label!r}")
        value = self.texts.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def confirm(self, question: str) -> bool:
        self.confirm_questions.append(question)
        if not self.confirms:
            raise AssertionError(f"fake confirmation queue is empty for {question!r}")
        return self.confirms.pop(0)

    def message(self, title: str, lines: Sequence[str]) -> None:
        self.messages.append((title, list(lines)))

    def message_with_review(self, title: str, lines: Sequence[str]) -> bool:
        self.reviews.append((title, list(lines)))
        return self.review_results.pop(0) if self.review_results else True

    def completion(self, state: Mapping[str, Any]) -> None:
        self.completions.append(state)

    def watch(self, state: Mapping[str, Any], _call: Any, _sleep_fn: Any) -> int:
        self.watched.append(state)
        return 0


class FakeController:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        request = dict(request)
        self.requests.append(request)
        action = request.get("action")
        if action == "status":
            return {"state": {"status": "idle", "phase": "idle"}}
        if action == "discover":
            return {
                "disks": [
                    {
                        "path": "/dev/sda",
                        "size": 128 * 1024**3,
                        "model": "target USB",
                        "eligible": True,
                        "transport": "usb",
                    },
                    {"path": "/dev/sdb", "size": 64 * 1024**3, "eligible": False, "reasons": ["protected key media"]},
                ]
            }
        if action == "plan":
            return {
                "target": {"path": "/dev/sda", "token": "TARGET-123"},
                "key": None,
                "payload": {"required_target_bytes": 1},
                "installer": {"revision": "test"},
            }
        if action == "submit":
            return {"state": {"status": "queued", "phase": "queued", "job_id": "job-1"}}
        raise AssertionError(f"unexpected controller action: {action}")


def _valid_settings(settings: Mapping[str, Any]) -> Mapping[str, Any]:
    return installed_target.validate_settings(dict(settings))


def _plain_form_choices() -> list[int]:
    # Home, target, account, network, Wi-Fi off, remote, SSH off, RDP off,
    # encryption, plain, review.
    return [0, 0, 0, 1, 0, 2, 0, 0, 3, 0, 4]


class InstallerUiTests(unittest.TestCase):
    def test_install_workflow_uses_fixed_protocol_and_real_validator(self) -> None:
        controller = FakeController()
        ui = FakeInteraction(
            choices=_plain_form_choices(),
            texts=[
                "sierra",
                "omarchy-pi",
                "Europe/London",
                "en_GB.UTF-8",
                "us",
                "account-secret",
                "account-secret",
                "TARGET-123",
                "YES",
            ],
            confirms=[True],
        )

        result = installer_ui.run(controller, _valid_settings, interaction=ui)
        self.assertEqual(result, 0)
        self.assertEqual([request["action"] for request in controller.requests], ["status", "status", "discover", "plan", "submit"])
        submit = controller.requests[-1]
        self.assertEqual(submit["target_confirmation"], "TARGET-123")
        self.assertTrue(submit["consent_internet"])
        settings = next(request["settings"] for request in controller.requests if request["action"] == "plan")
        self.assertEqual(settings["username"], "sierra")
        rendered = "\n".join(line for _title, lines in ui.messages + ui.reviews for line in lines)
        self.assertNotIn("account-secret", rendered)
        self.assertEqual(len(ui.watched), 1)

    def test_back_from_account_keeps_earlier_edits(self) -> None:
        ui = FakeInteraction(texts=["alice", installer_ui._BACK])
        app = installer_ui.InstallerUi(lambda _request: {}, _valid_settings, ui)
        values = app._base_settings({})
        app._edit_account(values)
        self.assertEqual(values["username"], "alice")
        self.assertEqual(values["timezone"], "Europe/London")

    def test_validation_error_returns_to_menu_without_reasking_fields(self) -> None:
        calls = 0

        def validate(settings: Mapping[str, Any]) -> Mapping[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError("hostname needs correction")
            return dict(settings)

        # Account, network, remote, encryption, review, review again.  The
        # second review succeeds without another text prompt.
        ui = FakeInteraction(
            choices=[0, 1, 0, 2, 0, 0, 3, 0, 4, 4],
            texts=["sierra", "omarchy-pi", "Europe/London", "en_GB.UTF-8", "us", "account-secret", "account-secret"],
        )
        app = installer_ui.InstallerUi(lambda _request: {}, validate, ui)
        result = app._collect_settings({})
        self.assertIsInstance(result, dict)
        self.assertEqual(calls, 2)
        self.assertEqual(result["hostname"], "omarchy-pi")

    def test_saved_modes_are_preselected_and_password_reuse_is_explicit(self) -> None:
        controller = FakeController()
        defaults = {
            "wifi": {"country": "GB", "ssid": "house", "password": "wifi-secret"},
            "ssh_enabled": False,
            "rdp_mode": "lan",
            "rdp_password": "rdp-secret",
        }
        ui = FakeInteraction(
            choices=[0, 1, 0, 0, 1, 1, 2, 0, 2, 3, 0, 4],
            texts=[
                "sierra", "omarchy-pi", "Europe/London", "en_GB.UTF-8", "us", "account-secret", "account-secret",
                "GB", "house", "wifi-secret", "rdp-secret", "TARGET-123", "YES",
            ],
            confirms=[True, True, True],
        )

        result = installer_ui.run(controller, _valid_settings, defaults=defaults, interaction=ui)
        self.assertEqual(result, 0)
        self.assertIn("Reuse the saved Wi-Fi password?", ui.confirm_questions)
        self.assertIn("Reuse the saved RDP password?", ui.confirm_questions)
        self.assertIn(("Wi-Fi", 1), ui.choose_calls)
        self.assertIn(("Remote desktop", 2), ui.choose_calls)
        settings = next(request["settings"] for request in controller.requests if request["action"] == "plan")
        self.assertEqual(settings["wifi"]["password"], "wifi-secret")
        self.assertEqual(settings["rdp_password"], "rdp-secret")
        rendered = "\n".join(line for _title, lines in ui.messages + ui.reviews for line in lines)
        self.assertNotIn("wifi-secret", rendered)
        self.assertNotIn("rdp-secret", rendered)

    def test_protected_disks_are_never_selectable(self) -> None:
        ui = FakeInteraction()
        app = installer_ui.InstallerUi(lambda _request: {}, _valid_settings, ui)
        result = app._select_disk([
            {"path": "/dev/sda", "size": 1_000_000, "reasons": ["mounted"]},
            {"path": "/dev/sdb", "size": 2_000_000, "eligible": False, "reasons": ["installer media"]},
        ])
        self.assertIs(result, installer_ui._CANCEL)
        self.assertFalse(ui.choose_calls)
        self.assertIn("mounted", " ".join(ui.messages[0][1]))

    def test_plain_recovery_skips_credentials_and_uses_repair_confirmation(self) -> None:
        requests: list[dict[str, Any]] = []

        def call(request: Mapping[str, Any]) -> Mapping[str, Any]:
            request = dict(request)
            requests.append(request)
            action = request["action"]
            if action == "status":
                return {"state": {"status": "idle"}}
            if action == "recovery-discover":
                return {"targets": [{"path": "/dev/sda", "size": 100, "transport": "usb", "eligible": True, "root_kind": "ext4", "luks_uuid": None, "token": "RECOVER TARGET"}]}
            if action == "recovery-plan":
                self.assertNotIn("passphrase", request)
                self.assertNotIn("key_file", request)
                return {"scope": "boot and initramfs only", "limitations": ["read-only plan"]}
            if action == "recovery-inspect":
                return {"inspection": {"read_only": True, "boot_files": ["cmdline.txt"]}}
            if action == "recovery-repair":
                self.assertEqual(request["repair_confirmation"], installer_ui.REPAIR_CONFIRMATION)
                return {"repaired": True}
            raise AssertionError(action)

        ui = FakeInteraction(choices=[1, 0], texts=["RECOVER TARGET"], confirms=[True])
        result = installer_ui.run(call, _valid_settings, interaction=ui)
        self.assertEqual(result, 0)
        self.assertEqual([request["action"] for request in requests], ["status", "recovery-discover", "recovery-plan", "recovery-inspect", "recovery-repair"])

    def test_completed_latest_job_uses_completion_card(self) -> None:
        def call(request: Mapping[str, Any]) -> Mapping[str, Any]:
            if request["action"] == "status":
                return {"state": {"status": "complete", "summary": {"hostname": "pi"}}}
            raise AssertionError(request)

        ui = FakeInteraction()
        app = installer_ui.InstallerUi(call, _valid_settings, ui)
        self.assertIsNone(app.latest_job())
        self.assertEqual(len(ui.completions), 1)

    def test_escape_cancels_saved_settings_choice(self):
        ui = FakeInteraction(choices=[installer_ui._BACK])
        app = installer_ui.InstallerUi(lambda _request: {}, _valid_settings, ui, defaults={"rdp_mode": "lan"})
        self.assertIs(app._reuse_defaults(), installer_ui._CANCEL)

    def test_declining_secret_reuse_then_cancelling_does_not_keep_old_secret(self):
        ui = FakeInteraction(choices=[1], texts=["GB", "house", installer_ui._BACK], confirms=[False])
        app = installer_ui.InstallerUi(lambda _request: {}, _valid_settings, ui)
        values = app._base_settings({"wifi": {"country": "GB", "ssid": "house", "password": "old-secret"}})
        app._edit_network(values)
        self.assertNotIn("password", values["wifi"])
        self.assertEqual(values["wifi"]["ssid"], "house")

    def test_noninteractive_launch_explains_ssh_tty_without_calling_controller(self):
        controller = mock.Mock()
        errors = io.StringIO()
        with mock.patch.object(installer_ui.sys.stdin, "isatty", return_value=False), mock.patch.object(installer_ui.sys, "stderr", errors):
            self.assertEqual(installer_ui.run(controller, _valid_settings), 1)
        self.assertIn("ssh -t", errors.getvalue())
        controller.assert_not_called()

    def test_status_failure_restores_blocking_input_for_error_screen(self):
        screen = mock.Mock()
        screen.getch.return_value = -1
        ui = installer_ui.CursesInteraction.__new__(installer_ui.CursesInteraction)
        ui.screen = screen
        ui._draw = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "connection lost"):
            ui.watch({"status": "running"}, mock.Mock(side_effect=RuntimeError("connection lost")), lambda _seconds: None)
        self.assertEqual(screen.nodelay.call_args_list, [mock.call(True), mock.call(False)])

    def test_renderer_reads_navigation_and_unicode_text_without_extra_adapter(self):
        ui = installer_ui.CursesInteraction.__new__(installer_ui.CursesInteraction)
        ui.screen = mock.Mock()
        ui.screen.get_wch.side_effect = [installer_ui.curses.KEY_DOWN, "\n", "ł"]
        self.assertEqual(ui._key(), installer_ui.curses.KEY_DOWN)
        self.assertEqual(ui._key(), 10)
        self.assertEqual(ui._key(), "ł")

    def test_small_terminal_can_scroll_to_last_instruction(self):
        ui = installer_ui.CursesInteraction.__new__(installer_ui.CursesInteraction)
        ui.screen = mock.Mock()
        ui.screen.getmaxyx.return_value = (15, 58)
        ui._draw = mock.Mock()
        ui._key = mock.Mock(side_effect=[installer_ui.curses.KEY_NPAGE, installer_ui.curses.KEY_NPAGE, 10])
        self.assertTrue(ui.message_with_review("Completion", [f"Instruction {i}" for i in range(20)]))
        self.assertIn("Instruction 19", ui._draw.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
