"""
Tests for Slack App Home dashboard.

Covers:
- View builder: status icons, service blocks, haniel block, overflow options, update section
- Action handlers: svc_menu_* overflow, update_repo_* buttons
- Error handling: status fetch failure, action failure
- Edge cases: self_update absent, haniel stop excluded
"""

import re
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from haniel.config.model import SlackBotConfig
from haniel.integrations.slack_bot import SlackBot


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_slack_config(**kwargs):
    defaults = {
        "bot_token": "xoxb-test",
        "app_token": "xapp-test",
        "notify_user": "U12345",
    }
    defaults.update(kwargs)
    return SlackBotConfig(**defaults)


def _make_status(
    services: dict | None = None,
    repos: dict | None = None,
    start_time: str | None = None,
    self_update: dict | None = "OMIT",
) -> dict:
    """Build a mock get_status() return value."""
    status = {
        "running": True,
        "start_time": start_time or "2026-03-20T10:00:00+00:00",
        "services": services or {},
        "repos": repos or {},
        "pending_restarts": [],
        "dependency_graph": {},
    }
    if self_update != "OMIT":
        status["self_update"] = self_update
    return status


def _make_service(state="running", uptime=3600.0, restart_count=0, ready="port:3104"):
    return {
        "state": state,
        "uptime": uptime,
        "restart_count": restart_count,
        "consecutive_failures": 0,
        "config": {"run": "python ...", "cwd": None, "repo": None, "after": [], "ready": ready, "enabled": True},
    }


class FakeAppHomeController:
    """Minimal fake satisfying the AppHomeController protocol."""

    def __init__(self, status=None):
        self._status = status or _make_status()
        self.restart_calls = []
        self.start_calls = []
        self.stop_calls = []
        self.enable_calls = []
        self.pull_calls = []
        self.self_update_calls = []
        self.restart_result = "restarted"

    def get_status(self) -> dict:
        return self._status

    def restart_service(self, name: str) -> str:
        self.restart_calls.append(name)
        return self.restart_result

    def start_service(self, name: str) -> None:
        self.start_calls.append(name)

    def stop_service(self, name: str) -> None:
        self.stop_calls.append(name)

    def enable_service(self, name: str) -> str:
        self.enable_calls.append(name)
        return f"Circuit reset for '{name}'"

    def trigger_pull(self, repo: str) -> None:
        self.pull_calls.append(repo)

    def approve_self_update(self) -> str:
        self.self_update_calls.append(True)
        return "Self-update approved."

    def request_restart(self) -> str:
        return "Restart initiated."


@pytest.fixture
def controller():
    """Default controller with some running services."""
    status = _make_status(
        services={
            "bot": _make_service(state="running", uptime=7200, restart_count=1, ready="port:3106"),
            "mcp-seosoyoung": _make_service(state="running", uptime=86400, ready="port:3104"),
            "rescue-bot": _make_service(state="stopped", uptime=0, ready="port:3107"),
        },
        self_update={"repo": "haniel", "pending": False, "auto_update": False},
    )
    return FakeAppHomeController(status)


@pytest.fixture
def bot_with_home(controller):
    """SlackBot with app_home_controller wired up."""
    config = _make_slack_config()
    with (
        patch("haniel.integrations.slack_bot.App") as MockApp,
        patch("haniel.integrations.slack_bot.SocketModeHandler"),
    ):
        mock_app = MagicMock()
        registered = {"events": {}, "actions": {}}

        def mock_event(event_name):
            def decorator(fn):
                registered["events"][event_name] = fn
                return fn
            return decorator

        def mock_action(action_id):
            def decorator(fn):
                registered["actions"][action_id if isinstance(action_id, str) else action_id.pattern] = fn
                return fn
            return decorator

        mock_app.event = mock_event
        mock_app.action = mock_action
        MockApp.return_value = mock_app

        bot = SlackBot(config, approve_callback=None, app_home_controller=controller)

    bot._registered = registered
    bot._client = MagicMock()
    bot._dm_channel = "D_TEST"
    return bot


# ── View builder tests ───────────────────────────────────────────────────────


class TestBuildHomeView:
    """Tests for _build_home_view and its sub-builders."""

    def test_returns_home_type(self, bot_with_home, controller):
        view = bot_with_home._build_home_view(controller.get_status())
        assert view["type"] == "home"
        assert "blocks" in view

    def test_header_block_present(self, bot_with_home, controller):
        view = bot_with_home._build_home_view(controller.get_status())
        headers = [b for b in view["blocks"] if b["type"] == "header"]
        assert len(headers) >= 1
        assert "하니엘" in headers[0]["text"]["text"] or "서비스" in headers[0]["text"]["text"]

    def test_haniel_block_is_first_service(self, bot_with_home, controller):
        """Haniel itself should appear as the first service row."""
        view = bot_with_home._build_home_view(controller.get_status())
        sections = [b for b in view["blocks"] if b["type"] == "section"]
        assert len(sections) >= 1
        first_section_text = sections[0]["text"]["text"]
        assert "haniel" in first_section_text.lower()

    def test_haniel_shows_running_icon(self, bot_with_home, controller):
        view = bot_with_home._build_home_view(controller.get_status())
        sections = [b for b in view["blocks"] if b["type"] == "section"]
        haniel_text = sections[0]["text"]["text"]
        assert "🟢" in haniel_text

    def test_service_state_icons(self, bot_with_home, controller):
        """Each service shows the correct state icon."""
        view = bot_with_home._build_home_view(controller.get_status())
        sections = [b for b in view["blocks"] if b["type"] == "section"]
        texts = {s["text"]["text"] for s in sections}

        # bot is running → 🟢
        bot_texts = [t for t in texts if "bot" in t and "mcp" not in t and "rescue" not in t]
        assert any("🟢" in t for t in bot_texts)

        # rescue-bot is stopped → ⚫
        rescue_texts = [t for t in texts if "rescue-bot" in t]
        assert any("⚫" in t for t in rescue_texts)

    def test_crashed_service_shows_red(self, bot_with_home):
        status = _make_status(
            services={"crasher": _make_service(state="crashed")},
        )
        view = bot_with_home._build_home_view(status)
        sections = [b for b in view["blocks"] if b["type"] == "section"]
        crasher_texts = [s["text"]["text"] for s in sections if "crasher" in s["text"]["text"]]
        assert any("🔴" in t for t in crasher_texts)

    def test_starting_service_shows_orange(self, bot_with_home):
        status = _make_status(
            services={"starter": _make_service(state="starting")},
        )
        view = bot_with_home._build_home_view(status)
        sections = [b for b in view["blocks"] if b["type"] == "section"]
        starter_texts = [s["text"]["text"] for s in sections if "starter" in s["text"]["text"]]
        assert any("🟠" in t for t in starter_texts)

    def test_service_shows_port_and_uptime(self, bot_with_home, controller):
        view = bot_with_home._build_home_view(controller.get_status())
        sections = [b for b in view["blocks"] if b["type"] == "section"]
        bot_section = [s for s in sections if "bot" in s["text"]["text"] and "rescue" not in s["text"]["text"] and "mcp" not in s["text"]["text"]]
        assert len(bot_section) >= 1
        text = bot_section[0]["text"]["text"]
        assert ":3106" in text


class TestOverflowOptions:
    """Tests for _build_overflow_options."""

    def test_running_has_restart(self, bot_with_home):
        options = bot_with_home._build_overflow_options("bot", "running")
        values = [o["value"] for o in options]
        assert "restart:bot" in values

    def test_running_has_stop_for_non_haniel(self, bot_with_home):
        options = bot_with_home._build_overflow_options("bot", "running")
        values = [o["value"] for o in options]
        assert "stop:bot" in values

    def test_haniel_running_has_no_stop(self, bot_with_home):
        """Haniel itself must not have a stop option."""
        options = bot_with_home._build_overflow_options("haniel", "running")
        values = [o["value"] for o in options]
        assert "restart:haniel" in values
        assert "stop:haniel" not in values

    def test_stopped_has_start(self, bot_with_home):
        options = bot_with_home._build_overflow_options("rescue-bot", "stopped")
        values = [o["value"] for o in options]
        assert "start:rescue-bot" in values

    def test_crashed_has_start(self, bot_with_home):
        options = bot_with_home._build_overflow_options("crasher", "crashed")
        values = [o["value"] for o in options]
        assert "start:crasher" in values

    def test_circuit_open_has_enable(self, bot_with_home):
        options = bot_with_home._build_overflow_options("broken", "circuit_open")
        values = [o["value"] for o in options]
        assert "start:broken" in values
        assert "enable:broken" in values


class TestUpdateSection:
    """Tests for update section visibility."""

    def test_no_updates_hides_section(self, bot_with_home):
        status = _make_status(
            repos={"haniel": {"pending_changes": None, "pulling": False}},
            self_update={"repo": "haniel", "pending": False},
        )
        view = bot_with_home._build_home_view(status)
        headers = [b for b in view["blocks"] if b["type"] == "header"]
        header_texts = [h["text"]["text"] for h in headers]
        assert not any("업데이트" in t for t in header_texts)

    def test_pending_changes_shows_update_button(self, bot_with_home):
        status = _make_status(
            repos={
                "seosoyoung": {
                    "pending_changes": {"commits": ["abc fix"], "stat": "1 file"},
                    "pulling": False,
                    "last_head": "abc1234",
                    "branch": "main",
                },
            },
        )
        view = bot_with_home._build_home_view(status)
        buttons = []
        for b in view["blocks"]:
            acc = b.get("accessory", {})
            if acc.get("type") == "button":
                buttons.append(acc)
        assert any("pull:seosoyoung" in btn.get("value", "") for btn in buttons)

    def test_self_update_pending_shows_haniel_update(self, bot_with_home):
        status = _make_status(
            self_update={"repo": "haniel", "pending": True},
            repos={"haniel": {"pending_changes": {"commits": ["feat: new"], "stat": ""}, "pulling": False}},
        )
        view = bot_with_home._build_home_view(status)
        buttons = []
        for b in view["blocks"]:
            acc = b.get("accessory", {})
            if acc.get("type") == "button":
                buttons.append(acc)
        assert any("update:haniel" in btn.get("value", "") for btn in buttons)

    def test_update_button_has_confirm(self, bot_with_home):
        status = _make_status(
            repos={
                "seosoyoung": {
                    "pending_changes": {"commits": ["abc fix"], "stat": "1 file"},
                    "pulling": False,
                },
            },
        )
        view = bot_with_home._build_home_view(status)
        buttons = []
        for b in view["blocks"]:
            acc = b.get("accessory", {})
            if acc.get("type") == "button":
                buttons.append(acc)
        assert any("confirm" in btn for btn in buttons)


class TestSelfUpdateAbsent:
    """Tests for when self_update key is missing from status."""

    def test_no_self_update_key_no_error(self, bot_with_home):
        """status without self_update key should not raise KeyError."""
        status = _make_status(self_update="OMIT")
        # Should not raise
        view = bot_with_home._build_home_view(status)
        assert view["type"] == "home"

    def test_haniel_block_still_present_without_self_update(self, bot_with_home):
        status = _make_status(self_update="OMIT")
        view = bot_with_home._build_home_view(status)
        sections = [b for b in view["blocks"] if b["type"] == "section"]
        assert any("haniel" in s["text"]["text"].lower() for s in sections)


class TestErrorView:
    """Tests for error view."""

    def test_error_view_structure(self, bot_with_home):
        view = bot_with_home._build_error_view("Connection refused")
        assert view["type"] == "home"
        texts = [b["text"]["text"] for b in view["blocks"] if b["type"] == "section"]
        assert any("Connection refused" in t for t in texts)


# ── Action handler tests ─────────────────────────────────────────────────────


class TestSvcMenuAction:
    """Tests for overflow menu action handler."""

    def test_restart_action_calls_controller(self, bot_with_home, controller):
        handler = None
        for key, fn in bot_with_home._registered["actions"].items():
            if "svc_menu" in key:
                handler = fn
                break
        assert handler is not None, "svc_menu handler not registered"

        ack = MagicMock()
        body = {
            "actions": [{"selected_option": {"value": "restart:bot"}}],
            "user": {"id": "U12345"},
        }
        client = MagicMock()
        handler(ack=ack, body=body, client=client, logger=MagicMock())

        ack.assert_called_once()
        assert "bot" in controller.restart_calls

    def test_start_action_calls_controller(self, bot_with_home, controller):
        handler = None
        for key, fn in bot_with_home._registered["actions"].items():
            if "svc_menu" in key:
                handler = fn
                break

        ack = MagicMock()
        body = {
            "actions": [{"selected_option": {"value": "start:rescue-bot"}}],
            "user": {"id": "U12345"},
        }
        handler(ack=ack, body=body, client=MagicMock(), logger=MagicMock())
        assert "rescue-bot" in controller.start_calls

    def test_stop_action_calls_controller(self, bot_with_home, controller):
        handler = None
        for key, fn in bot_with_home._registered["actions"].items():
            if "svc_menu" in key:
                handler = fn
                break

        ack = MagicMock()
        body = {
            "actions": [{"selected_option": {"value": "stop:mcp-seosoyoung"}}],
            "user": {"id": "U12345"},
        }
        handler(ack=ack, body=body, client=MagicMock(), logger=MagicMock())
        assert "mcp-seosoyoung" in controller.stop_calls

    def test_enable_action_calls_controller(self, bot_with_home, controller):
        handler = None
        for key, fn in bot_with_home._registered["actions"].items():
            if "svc_menu" in key:
                handler = fn
                break

        ack = MagicMock()
        body = {
            "actions": [{"selected_option": {"value": "enable:broken-svc"}}],
            "user": {"id": "U12345"},
        }
        handler(ack=ack, body=body, client=MagicMock(), logger=MagicMock())
        assert "broken-svc" in controller.enable_calls


class TestUpdateRepoAction:
    """Tests for update button action handler."""

    def test_pull_action_calls_trigger_pull(self, bot_with_home, controller):
        handler = None
        for key, fn in bot_with_home._registered["actions"].items():
            if "update_repo" in key:
                handler = fn
                break
        assert handler is not None, "update_repo handler not registered"

        ack = MagicMock()
        body = {
            "actions": [{"value": "pull:seosoyoung"}],
            "user": {"id": "U12345"},
        }
        handler(ack=ack, body=body, client=MagicMock(), logger=MagicMock())

        ack.assert_called_once()
        assert "seosoyoung" in controller.pull_calls

    def test_update_haniel_calls_self_update(self, bot_with_home, controller):
        handler = None
        for key, fn in bot_with_home._registered["actions"].items():
            if "update_repo" in key:
                handler = fn
                break

        ack = MagicMock()
        body = {
            "actions": [{"value": "update:haniel"}],
            "user": {"id": "U12345"},
        }
        handler(ack=ack, body=body, client=MagicMock(), logger=MagicMock())
        assert len(controller.self_update_calls) == 1


class TestActionErrorHandling:
    """Tests for error handling in action handlers."""

    def test_svc_menu_failure_sends_ephemeral(self, bot_with_home):
        handler = None
        for key, fn in bot_with_home._registered["actions"].items():
            if "svc_menu" in key:
                handler = fn
                break

        # Make the controller raise
        bot_with_home._app_home_controller.restart_service = MagicMock(side_effect=RuntimeError("oops"))

        ack = MagicMock()
        client = MagicMock()
        body = {
            "actions": [{"selected_option": {"value": "restart:bot"}}],
            "user": {"id": "U12345"},
        }
        handler(ack=ack, body=body, client=client, logger=MagicMock())
        client.chat_postEphemeral.assert_called_once()
        call_kwargs = client.chat_postEphemeral.call_args[1]
        assert "oops" in call_kwargs["text"]


# ── Handler registration tests ──────────────────────────────────────────────


class TestHandlerRegistration:
    """Tests that handlers are only registered when controller is provided."""

    def test_no_controller_no_home_handlers(self):
        config = _make_slack_config()
        with (
            patch("haniel.integrations.slack_bot.App") as MockApp,
            patch("haniel.integrations.slack_bot.SocketModeHandler"),
        ):
            mock_app = MagicMock()
            registered_events = []

            def mock_event(event_name):
                def decorator(fn):
                    registered_events.append(event_name)
                    return fn
                return decorator

            mock_app.event = mock_event
            mock_app.action = MagicMock(return_value=lambda fn: fn)
            MockApp.return_value = mock_app

            bot = SlackBot(config, approve_callback=None, app_home_controller=None)

        assert "app_home_opened" not in registered_events
