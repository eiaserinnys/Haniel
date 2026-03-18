"""
WebSocket event stream for the haniel dashboard.

Provides real-time events to dashboard clients:
- state_change: service state transitions
- repo_change: repository changes detected
- self_update_pending: self-update approval needed
- reload_complete: config reload finished
"""

import asyncio
import json
import logging
import weakref
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiohttp import web, WSMsgType

if TYPE_CHECKING:
    from ..core.runner import ServiceRunner
    from ..core.health import ServiceState

logger = logging.getLogger(__name__)


class DashboardWebSocket:
    """Manages WebSocket connections and broadcasts real-time events.

    Lifecycle:
    1. Created in setup_dashboard() and registered with HealthManager
    2. Each client connection is tracked in _clients set
    3. Events are broadcast to all connected clients
    4. On connection, current full status is sent as initial message
    """

    def __init__(self, runner: "ServiceRunner"):
        self.runner = runner
        self._clients: weakref.WeakSet[web.WebSocketResponse] = weakref.WeakSet()
        self._loop: asyncio.AbstractEventLoop | None = None

    def setup(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind to event loop and register HealthManager callback."""
        self._loop = loop
        self.runner.health_manager.add_callback(self._on_state_change)

    def _on_state_change(
        self,
        service_name: str,
        old_state: "ServiceState",
        new_state: "ServiceState",
    ) -> None:
        """Called by HealthManager when a service state changes.

        This runs on the runner's poll thread (sync), so we schedule
        the broadcast on the event loop thread.
        """
        event = {
            "type": "state_change",
            "service": service_name,
            "old": old_state.value,
            "new": new_state.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._schedule_broadcast(event)

    def broadcast_repo_change(self, repo_name: str, pending_changes: dict) -> None:
        """Broadcast a repo change event (call after fetch detects changes)."""
        event = {
            "type": "repo_change",
            "repo": repo_name,
            "pending_changes": pending_changes,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._schedule_broadcast(event)

    def broadcast_self_update_pending(self, repo: str) -> None:
        """Broadcast that a self-update is waiting for approval."""
        event = {
            "type": "self_update_pending",
            "repo": repo,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._schedule_broadcast(event)

    def broadcast_reload_complete(self) -> None:
        """Broadcast that config reload is complete."""
        event = {
            "type": "reload_complete",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._schedule_broadcast(event)

    def _schedule_broadcast(self, event: dict) -> None:
        """Thread-safe: schedule broadcast on the event loop."""
        if self._loop and not self._loop.is_closed():
            # Default-capture event to avoid late-binding closure issue
            self._loop.call_soon_threadsafe(
                lambda e=event: self._loop.create_task(self._broadcast(e))
            )

    async def _broadcast(self, event: dict) -> None:
        """Send an event to all connected WebSocket clients."""
        if not self._clients:
            return
        text = json.dumps(event)
        dead: list[web.WebSocketResponse] = []
        for ws in list(self._clients):
            try:
                if not ws.closed:
                    await ws.send_str(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Handle a WebSocket upgrade request at GET /ws."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        logger.info("WebSocket client connected")

        # Send current full status as initial message
        try:
            status = self.runner.get_status()
            await ws.send_str(
                json.dumps(
                    {
                        "type": "init",
                        "status": status,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
            )
        except Exception as e:
            logger.warning(f"Failed to send initial status: {e}")

        # Keep connection alive, handle pings and closes
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    logger.warning(
                        f"WebSocket error: {ws.exception()}"
                    )
                    break
                # Client messages are accepted but not processed
        finally:
            self._clients.discard(ws)
            logger.info("WebSocket client disconnected")

        return ws
