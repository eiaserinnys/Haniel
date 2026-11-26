"""
WebSocket event stream for the haniel dashboard.

Provides real-time events to dashboard clients:
- state_change: service state transitions
- repo_change: repository changes detected
- self_update_pending: self-update approval needed
- reload_complete: config reload finished
"""

import asyncio
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from starlette.websockets import WebSocket, WebSocketDisconnect

if TYPE_CHECKING:
    from ..core.runner import ServiceRunner
    from ..core.health import ServiceState
    from ..integrations.slack_bot import SlackBot

logger = logging.getLogger(__name__)


class DashboardWebSocket:
    """Manages WebSocket connections and broadcasts real-time events.

    Lifecycle:
    1. Created in setup_dashboard() and registered with HealthManager
    2. Each client connection is tracked in _clients set
    3. Events are broadcast to all connected clients
    4. On connection, current full status is sent as initial message
    """

    def __init__(
        self,
        runner: "ServiceRunner",
        token: str | None = None,
        slack_bot: "SlackBot | None" = None,
    ):
        """Construct the dashboard WS handler.

        Args:
            runner: ServiceRunner instance to expose via WS broadcast
            token: Bearer token required as a query-param on the WS upgrade
                URL (``/ws?token=...``). When ``None`` or empty, WS auth is
                disabled (noauth mode — matches AuthMiddleware short-circuit
                and the backward-compat path used by existing tests).
            slack_bot: Optional SlackBot that receives service crash notifications
        """
        self.runner = runner
        self._token = token or ""
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

        self._slack_bot = slack_bot

        # Auto-diagnosis: tracks services currently being diagnosed
        self._diagnosing_services: set[str] = set()

    def setup(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind to event loop and register health callbacks."""
        self._loop = loop
        self.runner.health_manager.add_callback(self._on_state_change)
        # If the runner consumed a self-update marker on startup, broadcast it
        # now that the loop is bound. Late-connecting clients also pick it up
        # via the init message (status.self_update.last_result).
        last = getattr(self.runner, "_last_self_update_result", None)
        if last is not None:
            self.broadcast_self_update_completed(last.to_dict())

    def _on_state_change(
        self,
        service_name: str,
        old_state: "ServiceState",
        new_state: "ServiceState",
    ) -> None:
        """Called by HealthManager when a service state changes.

        This runs on the runner's poll thread (sync), so we schedule
        the broadcast on the event loop thread.

        Also triggers auto-diagnosis when a service crashes, and clears
        the diagnosing flag when it recovers.
        """
        from ..core.health import ServiceState

        event = {
            "type": "state_change",
            "service": service_name,
            "old": old_state.value,
            "new": new_state.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._schedule_broadcast(event)

        # Auto-diagnosis: trigger on crash.
        # _diagnosing_services cleanup is handled exclusively by _run_diagnosis's finally block.
        if new_state in (ServiceState.CRASHED, ServiceState.CIRCUIT_OPEN):
            if service_name not in self._diagnosing_services:
                self._diagnosing_services.add(service_name)
                self._schedule_coroutine(self._run_diagnosis(service_name))

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

    def broadcast_self_update_started(self, repo: str) -> None:
        """Broadcast that an approved self-update is starting (server about to
        shut down). Canonical 'updating' signal for the dashboard overlay."""
        event = {
            "type": "self_update_started",
            "repo": repo,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._schedule_broadcast(event)

    def broadcast_self_update_completed(self, result: dict) -> None:
        """Broadcast the outcome of the previous self-update cycle.

        Called from setup() when the runner consumed a marker on startup.
        """
        event = {
            "type": "self_update_completed",
            "result": result,
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

    def _schedule_coroutine(self, coro) -> None:
        """Thread-safe: schedule a coroutine on the event loop."""
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(lambda c=coro: self._loop.create_task(c))

    def _schedule_broadcast(self, event: dict) -> None:
        """Thread-safe: schedule broadcast on the event loop."""
        if self._loop and not self._loop.is_closed():
            # Default-capture event to avoid late-binding closure issue
            self._loop.call_soon_threadsafe(
                lambda e=event: self._loop.create_task(self._broadcast(e))
            )

    async def _run_diagnosis(self, service_name: str) -> None:
        """Notify via Slack when a service crashes.

        Exceptions are caught to ensure _diagnosing_services cleanup always runs.
        """
        try:
            if self._slack_bot is not None:
                self._slack_bot.notify_crash(service_name)

        except Exception as e:
            logger.warning("Crash notification failed for %s: %s", service_name, e)
        finally:
            self._diagnosing_services.discard(service_name)

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
        """Handle a WebSocket upgrade request at GET /ws.

        Token check matches AuthMiddleware short-circuit: ``self._token``
        falsy means noauth mode. With a token configured we require
        ``?token=...`` query and constant-time compare it before accepting
        the upgrade. Mirrors the orchestrator-server pattern
        (``haniel_orch.hub._verify_dashboard_token``) — single rule both
        sides of the codebase.
        """
        if self._token:
            client_token = websocket.query_params.get("token")
            if not client_token or not hmac.compare_digest(client_token, self._token):
                await websocket.close(code=4001, reason="auth failed")
                return
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
