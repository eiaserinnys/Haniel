"""
WebSocket handler for the haniel dashboard chat panel.

Provides a /ws/chat endpoint that bridges the browser chat UI with
ClaudeSessionManager subprocess sessions.

Additionally relays user/assistant messages to Slack DM threads when
SlackBot and ChatBroadcaster are injected.

Client -> Server message types:
  {"type": "send_message", "session_id": "<uuid|null>", "text": "..."}
  {"type": "new_session"}
  {"type": "list_sessions"}
  {"type": "load_history", "session_id": "<uuid>"}

Server -> Client message types:
  {"type": "session_start", "session_id": "<uuid>", "is_new": true/false}
  {"type": "text_delta", "delta": "..."}
  {"type": "message_end", "session_id": "<uuid>"}
  {"type": "sessions_list", "sessions": [...]}
  {"type": "history", "session_id": "<uuid>", "messages": [...]}
  {"type": "error", "error": "..."}
"""

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from starlette.websockets import WebSocket, WebSocketDisconnect

from ..core.claude_session import ClaudeSessionManager

if TYPE_CHECKING:
    from ..integrations.slack_bot import SlackBot
    from .chat_broadcast import ChatBroadcaster

logger = logging.getLogger(__name__)


class ChatWebSocket:
    """Handles WebSocket connections for the dashboard chat panel.

    One instance is shared; each WebSocket connection is handled independently.
    Optional slack_bot and broadcaster enable bidirectional Slack relay.
    """

    def __init__(
        self,
        session_manager: ClaudeSessionManager,
        slack_bot: "SlackBot | None" = None,
        broadcaster: "ChatBroadcaster | None" = None,
    ):
        self._manager = session_manager
        self._slack_bot = slack_bot
        self._broadcaster = broadcaster

    async def handle_ws(self, websocket: WebSocket) -> None:
        """Handle a WebSocket upgrade request at GET /ws/chat."""
        await websocket.accept()
        logger.info("Chat WebSocket client connected")

        active_session_id: str | None = None

        try:
            while True:
                raw = await websocket.receive_text()
                new_active = await self._handle_message(websocket, raw)
                if new_active and new_active != active_session_id:
                    if self._broadcaster:
                        if active_session_id:
                            self._broadcaster.unregister(active_session_id, websocket)
                        self._broadcaster.register(new_active, websocket)
                    active_session_id = new_active
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning("Chat WebSocket error: %s", e)
        finally:
            if active_session_id and self._broadcaster:
                self._broadcaster.unregister(active_session_id, websocket)
            logger.info("Chat WebSocket client disconnected")

    async def _handle_message(self, ws: WebSocket, raw: str) -> str | None:
        """Handle one incoming WebSocket message.

        Returns the active session ID after handling, or None if not applicable.
        """
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await self._send(ws, {"type": "error", "error": "invalid JSON"})
            return None

        msg_type = msg.get("type")

        if msg_type == "list_sessions":
            sessions = self._manager.list_sessions()
            await self._send(ws, {"type": "sessions_list", "sessions": sessions})
            return None

        elif msg_type == "new_session":
            new_id = self._manager.create_session()
            await self._send(
                ws,
                {"type": "session_start", "session_id": new_id, "is_new": True},
            )
            return new_id

        elif msg_type == "send_message":
            return await self._handle_send_message(ws, msg)

        elif msg_type == "load_history":
            session_id = msg.get("session_id", "")
            messages = self._manager.get_history(session_id)
            await self._send(
                ws,
                {"type": "history", "session_id": session_id, "messages": messages},
            )
            return None

        else:
            await self._send(
                ws, {"type": "error", "error": f"unknown message type: {msg_type}"}
            )
            return None

    async def _handle_send_message(self, ws: WebSocket, msg: dict) -> str | None:
        """Process a send_message request with optional Slack relay.

        Behaviour:
        - session_id=null -> use last session (or new session if none exists)
        - session_id=<uuid> -> resume that session
        - text="" -> session switch only; send session_start and return
        - text=<non-empty> -> stream through claude, relay to slack if bound
        """
        raw_session_id: str | None = msg.get("session_id")
        text: str = msg.get("text", "")

        # Resolve session_id=null to last session
        if raw_session_id is None:
            last = self._manager.get_last_session()
            if last:
                raw_session_id = last["id"]

        # Session switch (empty text): just send session_start, skip claude
        if text == "":
            if raw_session_id:
                session = self._manager.get_session(raw_session_id)
                is_new = session is None
            else:
                raw_session_id = self._manager.create_session()
                is_new = True

            await self._send(
                ws,
                {
                    "type": "session_start",
                    "session_id": raw_session_id,
                    "is_new": is_new,
                },
            )
            return raw_session_id

        # Stream message through claude subprocess
        actual_session_id: str | None = None
        compaction_msg_ts: str | None = None
        buffer: list[str] = []
        user_msg_relayed = False

        async for event in self._manager.stream_message(raw_session_id, text):
            # Always send to WebSocket client first
            await self._send(ws, event)

            evt_type = event.get("type")

            if evt_type == "session_start":
                actual_session_id = event.get("session_id")
                is_new = event.get("is_new", False)

                # Create Slack thread for brand-new sessions
                if is_new and self._slack_bot and actual_session_id:
                    await self._maybe_create_slack_thread(actual_session_id)

                # Relay user message to Slack (once, after session is known)
                if not user_msg_relayed and self._slack_bot and actual_session_id:
                    channel, thread_ts = self._get_slack_ctx(actual_session_id)
                    if channel and thread_ts:
                        user_msg_relayed = True
                        try:
                            await asyncio.to_thread(
                                self._slack_bot.post_chat_message,
                                channel,
                                thread_ts,
                                f"*사용자가 대시보드에서 요청한 내용*\n> {text}",
                            )
                        except Exception as e:
                            logger.warning("Slack user message relay failed: %s", e)

            elif evt_type == "text_delta":
                buffer.append(event.get("delta", ""))

            elif evt_type == "message_end":
                full_text = "".join(buffer)
                buffer.clear()
                if full_text and self._slack_bot and actual_session_id:
                    channel, thread_ts = self._get_slack_ctx(actual_session_id)
                    if channel and thread_ts:
                        try:
                            await asyncio.to_thread(
                                self._slack_bot.post_chat_message,
                                channel,
                                thread_ts,
                                full_text,
                            )
                        except Exception as e:
                            logger.warning("Slack assistant message relay failed: %s", e)

            elif evt_type == "compact_start":
                if self._slack_bot and actual_session_id:
                    channel, thread_ts = self._get_slack_ctx(actual_session_id)
                    if channel and thread_ts:
                        try:
                            compaction_msg_ts = await asyncio.to_thread(
                                self._slack_bot.post_compaction_start,
                                channel,
                                thread_ts,
                            )
                        except Exception as e:
                            logger.warning("Slack compact_start relay failed: %s", e)

            elif evt_type == "compact_end":
                if compaction_msg_ts and self._slack_bot and actual_session_id:
                    channel, thread_ts = self._get_slack_ctx(actual_session_id)
                    if channel and thread_ts:
                        try:
                            await asyncio.to_thread(
                                self._slack_bot.update_compaction_done,
                                channel,
                                thread_ts,
                                compaction_msg_ts,
                            )
                        except Exception as e:
                            logger.warning("Slack compact_end relay failed: %s", e)
                        compaction_msg_ts = None

            elif evt_type == "error":
                if self._slack_bot and actual_session_id:
                    channel, thread_ts = self._get_slack_ctx(actual_session_id)
                    if channel and thread_ts:
                        try:
                            await asyncio.to_thread(
                                self._slack_bot.post_error,
                                channel,
                                thread_ts,
                                event.get("error", ""),
                            )
                        except Exception as e:
                            logger.warning("Slack error relay failed: %s", e)

            # Broadcast to other dashboard clients watching this session (slack-initiated)
            if self._broadcaster and actual_session_id:
                await self._broadcaster.broadcast(actual_session_id, event)

        return actual_session_id or raw_session_id

    async def _maybe_create_slack_thread(self, session_id: str) -> None:
        """Create a Slack DM thread for a new session if not already bound."""
        session = self._manager.get_session(session_id)
        if session is None or session.get("slack_thread_ts"):
            return
        try:
            thread_ts = await asyncio.to_thread(
                self._slack_bot.create_chat_thread,
                session_id,
                self._slack_bot._config.notify_user,
            )
            if thread_ts:
                channel_id = (
                    self._slack_bot._dm_channel
                    or self._slack_bot._config.notify_user
                )
                self._manager.update_slack_binding(session_id, thread_ts, channel_id)
        except Exception as e:
            logger.warning("Slack thread creation failed: %s", e)

    def _get_slack_ctx(self, session_id: str) -> tuple[str | None, str | None]:
        """Return (channel_id, thread_ts) for session, or (None, None)."""
        session = self._manager.get_session(session_id)
        if session is None:
            return None, None
        return session.get("slack_channel_id"), session.get("slack_thread_ts")

    @staticmethod
    async def _send(ws: WebSocket, data: dict) -> None:
        try:
            await ws.send_text(json.dumps(data))
        except Exception as exc:
            logger.debug("Failed to send WS message: %s", exc)
