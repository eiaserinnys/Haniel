"""
ChatBroadcaster: WebSocket client registry for slack→dashboard relay.
"""
import json
import logging
from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)

class ChatBroadcaster:
    """Registry of dashboard WebSocket clients keyed by chat session ID.

    All methods must be called from the async event loop.
    """
    def __init__(self) -> None:
        self._watchers: dict[str, set[WebSocket]] = {}

    def register(self, session_id: str, ws: WebSocket) -> None:
        self._watchers.setdefault(session_id, set()).add(ws)

    def unregister(self, session_id: str, ws: WebSocket) -> None:
        watchers = self._watchers.get(session_id)
        if watchers:
            watchers.discard(ws)
            if not watchers:
                del self._watchers[session_id]

    async def broadcast(
        self, session_id: str, event: dict, exclude: "WebSocket | None" = None
    ) -> None:
        """Broadcast to all watchers. Dead connections are auto-removed."""
        watchers = set(self._watchers.get(session_id, ()))
        if not watchers:
            return
        dead: set[WebSocket] = set()
        payload = json.dumps(event)
        for ws in watchers:
            if ws is exclude:
                continue
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.unregister(session_id, ws)
            logger.debug("Removed dead WebSocket for session %s", session_id)
