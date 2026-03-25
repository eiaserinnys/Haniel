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
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from starlette.websockets import WebSocket, WebSocketDisconnect

if TYPE_CHECKING:
    from ..core.runner import ServiceRunner
    from ..core.health import ServiceState
    from ..integrations.slack_bot import SlackBot
    from ..dashboard.chat_broadcast import ChatBroadcaster
    from ..core.claude_session import ClaudeSessionManager

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
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

        # Chat integration (set by configure_chat before setup())
        self._chat_slack_bot = None
        self._chat_broadcaster = None
        self._chat_session_manager = None

    def configure_chat(
        self,
        slack_bot: "SlackBot | None",
        broadcaster: "ChatBroadcaster | None",
        session_manager: "ClaudeSessionManager | None",
    ) -> None:
        """Store chat integration dependencies for later binding in setup(loop)."""
        self._chat_slack_bot = slack_bot
        self._chat_broadcaster = broadcaster
        self._chat_session_manager = session_manager

    def setup(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind to event loop, register health callbacks, and connect chat DM handler."""
        self._loop = loop
        self.runner.health_manager.add_callback(self._on_state_change)
        # Register Slack DM handler once the event loop is running
        if (
            self._chat_slack_bot is not None
            and self._chat_broadcaster is not None
            and self._chat_session_manager is not None
        ):
            try:
                self._chat_slack_bot._register_dm_handler(
                    loop, self._chat_session_manager, self._chat_broadcaster
                )
                logger.info("Slack DM chat handler registered")
            except Exception as e:
                logger.warning("Failed to register Slack DM chat handler: %s", e)

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

    def broadcast_repo_pulling(self, repo_name: str, is_pulling: bool) -> None:
        """Broadcast that a repo pull is in progress (or finished)."""
        event = {
            "type": "repo_pulling",
            "repo": repo_name,
            "is_pulling": is_pulling,
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
        dead: list[WebSocket] = []
        for ws in list(self._clients):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def handle_ws(self, websocket: WebSocket) -> None:
        """Handle a WebSocket upgrade request at GET /ws."""
        await websocket.accept()
        self._clients.add(websocket)
        logger.info("WebSocket client connected")

        # Send current full status as initial message
        try:
            status = self.runner.get_status()
            await websocket.send_text(
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
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning(f"WebSocket error: {e}")
        finally:
            self._clients.discard(websocket)
            logger.info("WebSocket client disconnected")
