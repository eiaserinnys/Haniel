"""
WebSocket handler for the haniel dashboard chat panel.

Provides a /ws/chat endpoint that bridges the browser chat UI with
ClaudeSessionManager subprocess sessions.

Client → Server message types:
  {"type": "send_message", "session_id": "<uuid|null>", "text": "..."}
  {"type": "new_session"}
  {"type": "list_sessions"}

Server → Client message types:
  {"type": "session_start", "session_id": "<uuid>", "is_new": true/false}
  {"type": "text_delta", "delta": "..."}
  {"type": "message_end", "session_id": "<uuid>"}
  {"type": "sessions_list", "sessions": [...]}
  {"type": "error", "error": "..."}
"""

import json
import logging

from aiohttp import web, WSMsgType

from ..core.claude_session import ClaudeSessionManager

logger = logging.getLogger(__name__)


class ChatWebSocket:
    """Handles WebSocket connections for the dashboard chat panel.

    One instance is shared; each WebSocket connection is handled independently
    (concurrent conversations are supported, each gets its own session context).
    """

    def __init__(self, session_manager: ClaudeSessionManager):
        self._manager = session_manager

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Handle a WebSocket upgrade request at GET /ws/chat."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        logger.info("Chat WebSocket client connected")

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._handle_message(ws, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    logger.warning("Chat WebSocket error: %s", ws.exception())
                    break
        finally:
            logger.info("Chat WebSocket client disconnected")

        return ws

    async def _handle_message(self, ws: web.WebSocketResponse, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await self._send(ws, {"type": "error", "error": "invalid JSON"})
            return

        msg_type = msg.get("type")

        if msg_type == "list_sessions":
            sessions = self._manager.list_sessions()
            await self._send(ws, {"type": "sessions_list", "sessions": sessions})

        elif msg_type == "new_session":
            # Force a new session registered in the manager; no claude call.
            new_id = self._manager.create_session()
            await self._send(ws, {
                "type": "session_start",
                "session_id": new_id,
                "is_new": True,
            })

        elif msg_type == "send_message":
            await self._handle_send_message(ws, msg)

        else:
            await self._send(ws, {"type": "error", "error": f"unknown message type: {msg_type}"})

    async def _handle_send_message(self, ws: web.WebSocketResponse, msg: dict) -> None:
        """Process a send_message request.

        Behaviour:
        - session_id=null → use last session (or new session if none exists)
        - session_id=<uuid> → resume that session
        - text="" → session switch only; send session_start and return (no claude call)
        - text=<non-empty> → stream message through claude subprocess
        """
        raw_session_id: str | None = msg.get("session_id")
        text: str = msg.get("text", "")

        # Resolve session_id=null to last session
        if raw_session_id is None:
            last = self._manager.get_last_session()
            if last:
                raw_session_id = last["id"]
            # else raw_session_id stays None → stream_message will create new session

        # Session switch (empty text): just send session_start, skip claude
        if text == "":
            if raw_session_id:
                session = self._manager.get_session(raw_session_id)
                is_new = session is None
            else:
                # No target session — create one so the client gets a usable ID
                raw_session_id = self._manager.create_session()
                is_new = True

            await self._send(ws, {
                "type": "session_start",
                "session_id": raw_session_id,
                "is_new": is_new,
            })
            return

        # Stream message through claude subprocess
        async for event in self._manager.stream_message(raw_session_id, text):
            await self._send(ws, event)

    @staticmethod
    async def _send(ws: web.WebSocketResponse, data: dict) -> None:
        if not ws.closed:
            try:
                await ws.send_str(json.dumps(data))
            except Exception as exc:
                logger.debug("Failed to send WS message: %s", exc)
