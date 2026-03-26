"""
Phase 2 tests: bidirectional Slack chat relay + compaction notifications.

Covers:
- ChatBroadcaster: register, unregister, broadcast, dead-connection cleanup
- ClaudeSessionManager: get_session_by_thread_ts, update_slack_binding
- SlackBot chat methods: create_chat_thread, post_chat_message,
  post_compaction_start, update_compaction_done, post_error
- SlackBot._handle_dm_async: new session, existing session, compaction, error
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from haniel.core.claude_session import ClaudeSessionManager
from haniel.dashboard.chat_broadcast import ChatBroadcaster
from haniel.integrations.slack_bot import SlackBot
from haniel.config.model import SlackBotConfig


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_runner(tmp_path: Path) -> MagicMock:
    runner = MagicMock()
    runner.config_dir = tmp_path
    runner.config.mcp = None
    return runner


def _make_slack_config(**kwargs):
    defaults = {
        "bot_token": "xoxb-test",
        "app_token": "xapp-test",
        "notify_user": "U12345",
    }
    defaults.update(kwargs)
    return SlackBotConfig(**defaults)


@pytest.fixture
def mock_web_client():
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "D_TEST"}}
    client.chat_postMessage.return_value = {"ts": "111.001"}
    client.chat_update.return_value = {"ok": True}
    client.chat_delete.return_value = {"ok": True}
    return client


@pytest.fixture
def slack_bot(mock_web_client):
    config = _make_slack_config()
    with (
        patch("haniel.integrations.slack_bot.App"),
        patch("haniel.integrations.slack_bot.SocketModeHandler"),
    ):
        bot = SlackBot(config)
    bot._client = mock_web_client
    bot._dm_channel = "D_TEST"
    return bot


# ── ChatBroadcaster ────────────────────────────────────────────────────────────


class TestChatBroadcaster:
    def test_register_and_unregister(self):
        """register adds a WS; unregister removes it."""
        broadcaster = ChatBroadcaster()
        ws = MagicMock()
        broadcaster.register("sess-1", ws)
        assert ws in broadcaster._watchers["sess-1"]

        broadcaster.unregister("sess-1", ws)
        assert "sess-1" not in broadcaster._watchers

    def test_unregister_nonexistent_is_safe(self):
        """unregister on unknown session/ws does not raise."""
        broadcaster = ChatBroadcaster()
        ws = MagicMock()
        broadcaster.unregister("no-such-session", ws)  # no exception

    @pytest.mark.asyncio
    async def test_broadcast_sends_json_to_watchers(self):
        """broadcast sends JSON-encoded event to all registered WS clients."""
        broadcaster = ChatBroadcaster()
        ws1 = MagicMock()
        ws1.send_text = AsyncMock()
        ws2 = MagicMock()
        ws2.send_text = AsyncMock()

        broadcaster.register("sess-a", ws1)
        broadcaster.register("sess-a", ws2)

        event = {"type": "text_delta", "delta": "hello"}
        await broadcaster.broadcast("sess-a", event)

        expected = json.dumps(event)
        ws1.send_text.assert_awaited_once_with(expected)
        ws2.send_text.assert_awaited_once_with(expected)

    @pytest.mark.asyncio
    async def test_broadcast_removes_dead_connections(self):
        """Dead WebSocket connections are auto-removed during broadcast."""
        broadcaster = ChatBroadcaster()
        dead_ws = MagicMock()
        dead_ws.send_text = AsyncMock(side_effect=Exception("disconnected"))
        live_ws = MagicMock()
        live_ws.send_text = AsyncMock()

        broadcaster.register("sess-b", dead_ws)
        broadcaster.register("sess-b", live_ws)

        await broadcaster.broadcast("sess-b", {"type": "ping"})

        # dead connection should be removed
        watchers = broadcaster._watchers.get("sess-b", set())
        assert dead_ws not in watchers
        assert live_ws in watchers

    @pytest.mark.asyncio
    async def test_broadcast_to_empty_session_is_safe(self):
        """broadcast on a session with no watchers does not raise."""
        broadcaster = ChatBroadcaster()
        await broadcaster.broadcast("no-watchers", {"type": "ping"})  # no exception


# ── ClaudeSessionManager: Slack binding ───────────────────────────────────────


class TestSessionSlackBinding:
    def test_get_session_by_thread_ts_found(self, tmp_path):
        """get_session_by_thread_ts returns the matching session."""
        manager = ClaudeSessionManager(_make_runner(tmp_path))
        session_id = manager.create_session()
        manager.update_slack_binding(session_id, "111.222", "C_CHAN")

        found = manager.get_session_by_thread_ts("111.222")
        assert found is not None
        assert found["id"] == session_id

    def test_get_session_by_thread_ts_not_found(self, tmp_path):
        """get_session_by_thread_ts returns None when no match."""
        manager = ClaudeSessionManager(_make_runner(tmp_path))
        manager.create_session()

        result = manager.get_session_by_thread_ts("no-such-ts")
        assert result is None

    def test_update_slack_binding_persists(self, tmp_path):
        """update_slack_binding sets slack_thread_ts and slack_channel_id."""
        manager = ClaudeSessionManager(_make_runner(tmp_path))
        session_id = manager.create_session()
        manager.update_slack_binding(session_id, "999.000", "D_CH")

        session = manager.get_session(session_id)
        assert session["slack_thread_ts"] == "999.000"
        assert session["slack_channel_id"] == "D_CH"

    def test_update_slack_binding_unknown_session_is_safe(self, tmp_path):
        """update_slack_binding on unknown session logs warning and does not raise."""
        manager = ClaudeSessionManager(_make_runner(tmp_path))
        # Should not raise
        manager.update_slack_binding("no-such-id", "ts", "channel")


# ── SlackBot chat methods ──────────────────────────────────────────────────────


class TestSlackBotChatMethods:
    def test_create_chat_thread_returns_ts(self, slack_bot, mock_web_client):
        """create_chat_thread posts a message and returns ts."""
        ts = slack_bot.create_chat_thread("sess-1", "U_USER")
        assert ts == "111.001"
        mock_web_client.chat_postMessage.assert_called_once()
        call_kwargs = mock_web_client.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == "U_USER"

    def test_create_chat_thread_returns_none_on_error(self, slack_bot, mock_web_client):
        """create_chat_thread returns None when Slack API fails."""
        mock_web_client.chat_postMessage.side_effect = Exception("api error")
        ts = slack_bot.create_chat_thread("sess-1", "U_USER")
        assert ts is None

    def test_post_chat_message(self, slack_bot, mock_web_client):
        """post_chat_message sends message to thread."""
        slack_bot.post_chat_message("D_CHAN", "111.000", "hello there")
        mock_web_client.chat_postMessage.assert_called_once()
        call_kwargs = mock_web_client.chat_postMessage.call_args[1]
        assert call_kwargs["thread_ts"] == "111.000"
        assert call_kwargs["text"] == "hello there"

    def test_post_chat_message_swallows_errors(self, slack_bot, mock_web_client):
        """post_chat_message does not raise on Slack API failure."""
        mock_web_client.chat_postMessage.side_effect = Exception("fail")
        slack_bot.post_chat_message("D_CHAN", "111.000", "text")  # no exception

    def test_post_compaction_start_returns_ts(self, slack_bot, mock_web_client):
        """post_compaction_start posts notice and returns ts."""
        ts = slack_bot.post_compaction_start("D_CHAN", "111.000")
        assert ts == "111.001"
        call_kwargs = mock_web_client.chat_postMessage.call_args[1]
        assert "컴팩션" in call_kwargs["text"]

    def test_post_compaction_start_returns_none_on_error(self, slack_bot, mock_web_client):
        """post_compaction_start returns None on failure."""
        mock_web_client.chat_postMessage.side_effect = Exception("fail")
        ts = slack_bot.post_compaction_start("D_CHAN", "111.000")
        assert ts is None

    def test_update_compaction_done(self, slack_bot, mock_web_client):
        """update_compaction_done calls chat_update on the given msg_ts."""
        slack_bot.update_compaction_done("D_CHAN", "111.000", "111.999")
        mock_web_client.chat_update.assert_called_once()
        call_kwargs = mock_web_client.chat_update.call_args[1]
        assert call_kwargs["ts"] == "111.999"
        assert "완료" in call_kwargs["text"]

    def test_update_compaction_done_swallows_errors(self, slack_bot, mock_web_client):
        """update_compaction_done does not raise on failure."""
        mock_web_client.chat_update.side_effect = Exception("fail")
        slack_bot.update_compaction_done("D_CHAN", "111.000", "111.999")  # no exception

    def test_post_error(self, slack_bot, mock_web_client):
        """post_error posts error message to thread."""
        slack_bot.post_error("D_CHAN", "111.000", "something broke")
        call_kwargs = mock_web_client.chat_postMessage.call_args[1]
        assert "something broke" in call_kwargs["text"]
        assert call_kwargs["thread_ts"] == "111.000"

    def test_post_error_swallows_errors(self, slack_bot, mock_web_client):
        """post_error does not raise on Slack API failure."""
        mock_web_client.chat_postMessage.side_effect = Exception("fail")
        slack_bot.post_error("D_CHAN", "111.000", "error msg")  # no exception


# ── SlackBot._handle_dm_async ──────────────────────────────────────────────────


def _make_stream(*events):
    """Create an async generator that yields the given events."""

    async def _gen(session_id, text):
        for evt in events:
            yield evt

    return _gen


class TestHandleDmAsync:
    @pytest.mark.asyncio
    async def test_new_dm_creates_session(self, tmp_path, slack_bot):
        """Top-level DM (no thread_ts) creates a new session and binds it."""
        session_manager = ClaudeSessionManager(_make_runner(tmp_path))
        broadcaster = ChatBroadcaster()

        session_manager.stream_message = _make_stream(
            {"type": "text_delta", "delta": "hi"},
            {"type": "message_end"},
        )

        event = {
            "type": "message",
            "channel_type": "im",
            "channel": "D_CHAN",
            "ts": "200.001",
            "text": "hello",
        }
        await slack_bot._handle_dm_async(session_manager, broadcaster, event)

        # Session should be bound to thread_ts = ts (top-level DM)
        session = session_manager.get_session_by_thread_ts("200.001")
        assert session is not None

    @pytest.mark.asyncio
    async def test_thread_reply_resumes_existing_session(self, tmp_path, slack_bot):
        """Reply in an existing thread resumes the bound session."""
        session_manager = ClaudeSessionManager(_make_runner(tmp_path))
        broadcaster = ChatBroadcaster()

        session_id = session_manager.create_session()
        session_manager.update_slack_binding(session_id, "100.000", "D_CHAN")

        streamed_session_ids = []

        async def mock_stream(sid, text):
            streamed_session_ids.append(sid)
            yield {"type": "message_end"}

        session_manager.stream_message = mock_stream

        event = {
            "type": "message",
            "channel_type": "im",
            "channel": "D_CHAN",
            "ts": "100.002",
            "thread_ts": "100.000",
            "text": "follow-up",
        }
        await slack_bot._handle_dm_async(session_manager, broadcaster, event)

        # Same session should be reused
        assert streamed_session_ids == [session_id]

    @pytest.mark.asyncio
    async def test_assistant_message_posted_to_slack(self, tmp_path, slack_bot, mock_web_client):
        """Full text assembled from text_delta is posted to Slack at message_end."""
        session_manager = ClaudeSessionManager(_make_runner(tmp_path))
        broadcaster = ChatBroadcaster()

        session_manager.stream_message = _make_stream(
            {"type": "text_delta", "delta": "Hello "},
            {"type": "text_delta", "delta": "world"},
            {"type": "message_end"},
        )

        event = {
            "channel_type": "im",
            "channel": "D_CHAN",
            "ts": "300.001",
            "text": "hi",
        }
        await slack_bot._handle_dm_async(session_manager, broadcaster, event)

        # post_chat_message should be called with assembled text
        mock_web_client.chat_postMessage.assert_called()
        last_call = mock_web_client.chat_postMessage.call_args_list[-1][1]
        assert last_call["text"] == "Hello world"

    @pytest.mark.asyncio
    async def test_compaction_events_relay(self, tmp_path, slack_bot, mock_web_client):
        """compact_start posts notice; compact_end updates it."""
        session_manager = ClaudeSessionManager(_make_runner(tmp_path))
        broadcaster = ChatBroadcaster()

        mock_web_client.chat_postMessage.return_value = {"ts": "compaction-ts"}

        session_manager.stream_message = _make_stream(
            {"type": "compact_start"},
            {"type": "compact_end"},
            {"type": "message_end"},
        )

        event = {"channel_type": "im", "channel": "D_CHAN", "ts": "400.001", "text": "go"}
        await slack_bot._handle_dm_async(session_manager, broadcaster, event)

        # compact_start: chat_postMessage with compaction text
        # compact_end: chat_update to replace it
        assert mock_web_client.chat_update.called
        update_kwargs = mock_web_client.chat_update.call_args[1]
        assert update_kwargs["ts"] == "compaction-ts"

    @pytest.mark.asyncio
    async def test_error_event_posts_to_slack(self, tmp_path, slack_bot, mock_web_client):
        """error event posts error message to Slack thread."""
        session_manager = ClaudeSessionManager(_make_runner(tmp_path))
        broadcaster = ChatBroadcaster()

        session_manager.stream_message = _make_stream(
            {"type": "error", "error": "Claude timeout"},
        )

        event = {"channel_type": "im", "channel": "D_CHAN", "ts": "500.001", "text": "run"}
        await slack_bot._handle_dm_async(session_manager, broadcaster, event)

        last_call = mock_web_client.chat_postMessage.call_args_list[-1][1]
        assert "Claude timeout" in last_call["text"]

    @pytest.mark.asyncio
    async def test_events_broadcast_to_dashboard(self, tmp_path, slack_bot):
        """All stream events are broadcast to ChatBroadcaster."""
        session_manager = ClaudeSessionManager(_make_runner(tmp_path))
        broadcaster = ChatBroadcaster()

        ws = MagicMock()
        received: list[dict] = []

        async def mock_send(payload: str):
            received.append(json.loads(payload))

        ws.send_text = mock_send

        session_manager.stream_message = _make_stream(
            {"type": "text_delta", "delta": "x"},
            {"type": "message_end"},
        )

        event = {"channel_type": "im", "channel": "D_CHAN", "ts": "600.001", "text": "test"}
        # Register ws for the session that will be created
        # We need to intercept session creation to register ws
        original_create = session_manager.create_session

        def create_and_register():
            sid = original_create()
            broadcaster.register(sid, ws)
            return sid

        session_manager.create_session = create_and_register

        await slack_bot._handle_dm_async(session_manager, broadcaster, event)

        types = [e["type"] for e in received]
        assert "text_delta" in types
        assert "message_end" in types

    @pytest.mark.asyncio
    async def test_empty_text_is_ignored(self, tmp_path, slack_bot, mock_web_client):
        """DM events with empty text are silently ignored."""
        session_manager = ClaudeSessionManager(_make_runner(tmp_path))
        broadcaster = ChatBroadcaster()

        event = {"channel_type": "im", "channel": "D_CHAN", "ts": "700.001", "text": "  "}
        await slack_bot._handle_dm_async(session_manager, broadcaster, event)

        # No session created, no message posted
        assert len(session_manager.list_sessions()) == 0
        mock_web_client.chat_postMessage.assert_not_called()
