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

        # Auto-diagnosis: tracks services currently being diagnosed
        self._diagnosing_services: set[str] = set()

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
            self._loop.call_soon_threadsafe(
                lambda c=coro: self._loop.create_task(c)
            )

    def _schedule_broadcast(self, event: dict) -> None:
        """Thread-safe: schedule broadcast on the event loop."""
        if self._loop and not self._loop.is_closed():
            # Default-capture event to avoid late-binding closure issue
            self._loop.call_soon_threadsafe(
                lambda e=event: self._loop.create_task(self._broadcast(e))
            )

    async def _run_diagnosis(self, service_name: str) -> None:
        """Notify via Slack when a service crashes.

        Auto-diagnosis via Claude Code session is temporarily disabled.
        Sends a crash notification instead.
        Exceptions are caught to ensure _diagnosing_services cleanup always runs.
        """
        try:
            if self._chat_slack_bot is not None:
                self._chat_slack_bot.notify_crash(service_name)

            # ── Auto-diagnosis via Claude Code session (disabled) ──────────────
            # if self._chat_session_manager is None:
            #     return
            #
            # prompt = (
            #     f"하니엘 서비스 '{service_name}'이 다운되었습니다. "
            #     "원인을 진단하고 해결 방법을 제안해주세요."
            # )
            # session_id = self._chat_session_manager.create_session()
            #
            # # Bind to Slack thread if bot is available
            # if self._chat_slack_bot is not None and self._chat_slack_bot._dm_channel:
            #     dm_channel = self._chat_slack_bot._dm_channel
            #     thread_ts = self._chat_slack_bot.create_chat_thread(
            #         session_id, dm_channel
            #     )
            #     if thread_ts:
            #         self._chat_session_manager.update_slack_binding(
            #             session_id, thread_ts, dm_channel
            #         )
            #
            # compaction_msg_ts: str | None = None
            # buffer: list[str] = []
            #
            # async for evt in self._chat_session_manager.stream_message(session_id, prompt):
            #     evt_type = evt.get("type")
            #
            #     if evt_type == "text_delta":
            #         buffer.append(evt.get("delta", ""))
            #
            #     elif evt_type == "message_end":
            #         full_text = "".join(buffer)
            #         if full_text and self._chat_slack_bot is not None:
            #             session = self._chat_session_manager.get_session(session_id)
            #             if session and session.get("slack_thread_ts"):
            #                 self._chat_slack_bot.post_chat_message(
            #                     session["slack_channel_id"],
            #                     session["slack_thread_ts"],
            #                     full_text,
            #                 )
            #         buffer.clear()
            #
            #     elif evt_type == "compact_start":
            #         if self._chat_slack_bot is not None:
            #             session = self._chat_session_manager.get_session(session_id)
            #             if session and session.get("slack_thread_ts"):
            #                 compaction_msg_ts = self._chat_slack_bot.post_compaction_start(
            #                     session["slack_channel_id"], session["slack_thread_ts"]
            #                 )
            #
            #     elif evt_type == "compact_end":
            #         if compaction_msg_ts and self._chat_slack_bot is not None:
            #             session = self._chat_session_manager.get_session(session_id)
            #             if session and session.get("slack_thread_ts"):
            #                 self._chat_slack_bot.update_compaction_done(
            #                     session["slack_channel_id"],
            #                     session["slack_thread_ts"],
            #                     compaction_msg_ts,
            #                 )
            #             compaction_msg_ts = None
            #
            #     elif evt_type == "error":
            #         if self._chat_slack_bot is not None:
            #             session = self._chat_session_manager.get_session(session_id)
            #             if session and session.get("slack_thread_ts"):
            #                 self._chat_slack_bot.post_error(
            #                     session["slack_channel_id"],
            #                     session["slack_thread_ts"],
            #                     evt.get("error", ""),
            #                 )
            #
            #     # Broadcast all events to watching dashboard WS clients
            #     if self._chat_broadcaster is not None:
            #         await self._chat_broadcaster.broadcast(session_id, evt)

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
