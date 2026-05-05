"""Orchestrator WebSocket client for Haniel node agents.

Runs in a background thread with its own asyncio event loop.
Maintains a persistent WebSocket connection to the orchestrator server
with exponential backoff reconnection.

If the orchestrator is unreachable, all operations degrade gracefully
— the node continues to operate independently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ..config.model import OrchestratorClientConfig

logger = logging.getLogger(__name__)


class OrchestratorClient:
    """Node-side orchestrator WebSocket client.

    Runs a background thread with an asyncio event loop that maintains
    a persistent WebSocket connection to the orchestrator server.
    Reconnects with exponential backoff on disconnection.
    """

    def __init__(
        self,
        config: "OrchestratorClientConfig",
        haniel_version: str,
        get_services_info: "Callable[[], list[dict]] | None" = None,
        service_command_handler: "Callable[[str, str], None] | None" = None,
        deploy_approval_handler: "Callable[[str, str, str], str | None] | None" = None,
    ) -> None:
        self._config = config
        self._haniel_version = haniel_version
        self._get_services_info = get_services_info
        self._service_command_handler = service_command_handler
        # handler convention: (deploy_id, repo, branch) -> None | "deferred".
        #   None       — synchronous success; we send DeployResult{success}.
        #   "deferred" — handler scheduled the work elsewhere (e.g. self-update
        #                via deferred stop); DeployResult is sent on next
        #                startup via enqueue_deploy_result.
        #   Exception  — failure; we send DeployResult{failed, error=str(e)}.
        self._deploy_approval_handler = deploy_approval_handler
        self._ws = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._connected = False
        self._reconnect_delay = config.reconnect_base
        # Buffered DeployResults — flushed on every successful (re)connect.
        # Used for self-update results that survive runner restart.
        self._pending_deploy_results: list[dict] = []
        self._pending_lock = threading.Lock()

    def start(self) -> None:
        """Start the background WebSocket connection thread."""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="orch-client",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the background thread and close the connection."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def notify_change(
        self,
        repo: str,
        branch: str,
        commits: list[str],
        affected_services: list[str],
        diff_stat: str | None = None,
    ) -> None:
        """Notify the orchestrator of detected changes. Thread-safe.

        If not connected, the notification is silently dropped
        (graceful degradation — the node operates independently).
        """
        if not self._connected or not self._ws or not self._loop:
            return

        if not commits:
            return  # Nothing to notify

        # Build deterministic deploy_id
        first_commit_hash = commits[0].split()[0] if commits[0] else ""
        deploy_id = f"{self._config.node_id}:{repo}:{branch}:{first_commit_hash}"

        msg = {
            "type": "change_notification",
            "deploy_id": deploy_id,
            "node_id": self._config.node_id,
            "repo": repo,
            "branch": branch,
            "commits": commits,
            "affected_services": affected_services,
            "diff_stat": diff_stat,
            "detected_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            asyncio.run_coroutine_threadsafe(
                self._send_json(msg), self._loop
            )
        except Exception as e:
            logger.debug(f"Failed to queue change notification: {e}")

    def enqueue_deploy_result(
        self,
        deploy_id: str,
        status: str,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Buffer a DeployResult to send on the next successful connection.

        Used by runner.start() to report self-update results that completed
        during the previous wrapper iteration. Thread-safe.
        """
        msg = {
            "type": "deploy_result",
            "deploy_id": deploy_id,
            "node_id": self._config.node_id,
            "status": status,
            "error": error,
            "duration_ms": duration_ms,
        }
        with self._pending_lock:
            self._pending_deploy_results.append(msg)
        # If already connected, kick a flush on the loop
        if self._connected and self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._flush_pending_deploy_results(), self._loop
                )
            except Exception as e:
                logger.debug(f"Failed to schedule deploy result flush: {e}")

    async def _flush_pending_deploy_results(self) -> None:
        """Send all buffered DeployResults. Re-queue on send failure."""
        with self._pending_lock:
            pending = list(self._pending_deploy_results)
            self._pending_deploy_results.clear()
        for msg in pending:
            try:
                await self._send_json(msg)
            except Exception as e:
                logger.warning(
                    f"Failed to send buffered deploy result "
                    f"{msg.get('deploy_id')}: {e}"
                )
                # Re-queue this message and stop processing further messages
                # to preserve order. The next connect will flush again.
                with self._pending_lock:
                    self._pending_deploy_results.append(msg)
                break

    def _run_loop(self) -> None:
        """Entry point for the background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        except Exception as e:
            logger.error(f"Orchestrator client loop crashed: {e}")
        finally:
            self._loop.close()
            self._loop = None

    async def _run(self) -> None:
        """Main loop: connect → listen → reconnect with backoff."""
        import websockets

        while not self._stop_event.is_set():
            try:
                await self._connect_and_listen()
            except Exception as e:
                if self._stop_event.is_set():
                    break
                logger.warning(
                    f"Orchestrator connection failed: {e}. "
                    f"Reconnecting in {self._reconnect_delay:.1f}s"
                )
            finally:
                self._connected = False
                self._ws = None

            if self._stop_event.is_set():
                break

            # Backoff wait (interruptible)
            delay = self._next_backoff()
            for _ in range(int(delay * 10)):
                if self._stop_event.is_set():
                    return
                await asyncio.sleep(0.1)

    async def _connect_and_listen(self) -> None:
        """Connect to orchestrator, send NodeHello, and listen for messages."""
        import websockets

        async with websockets.connect(self._config.url) as ws:
            self._ws = ws

            # Send NodeHello
            hello = {
                "type": "node_hello",
                "node_id": self._config.node_id,
                "token": self._config.token,
                "hostname": platform.node(),
                "os": platform.system(),
                "arch": platform.machine(),
                "haniel_version": self._haniel_version,
                "services": self._get_services_info() if self._get_services_info else None,
            }
            await ws.send(json.dumps(hello))

            self._connected = True
            self._reset_backoff()
            logger.info(f"Connected to orchestrator at {self._config.url}")

            # Flush any DeployResults buffered during the previous startup
            # (e.g. self-update results). Order matters: must complete before
            # listener/heartbeat tasks start so the result reaches the server
            # before the connection can be torn down.
            await self._flush_pending_deploy_results()

            # Run heartbeat and listener concurrently
            listener = asyncio.create_task(self._listen(ws))
            heartbeat = asyncio.create_task(self._heartbeat_loop(ws))
            try:
                # Wait for either to finish (disconnect or stop)
                done, pending = await asyncio.wait(
                    [listener, heartbeat],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                # Re-raise if listener had an exception
                for task in done:
                    task.result()
            except asyncio.CancelledError:
                pass

    async def _listen(self, ws) -> None:
        """Listen for server messages on the WebSocket."""
        async for raw in ws:
            if self._stop_event.is_set():
                break
            try:
                msg = json.loads(raw)
                await self._handle_server_message(msg)
            except Exception as e:
                logger.warning(f"Error handling orchestrator message: {e}")

    async def _heartbeat_loop(self, ws) -> None:
        """Send periodic heartbeats to keep the connection alive."""
        while not self._stop_event.is_set():
            await asyncio.sleep(30)
            if self._stop_event.is_set():
                break
            try:
                await self._send_heartbeat()
            except Exception as e:
                logger.debug(f"Heartbeat send failed: {e}")
                break

    async def _handle_server_message(self, msg: dict) -> None:
        """Handle messages from the orchestrator server."""
        msg_type = msg.get("type")
        if msg_type == "deploy_approval":
            await self._handle_deploy_approval(msg)
        elif msg_type == "deploy_reject":
            logger.info(
                f"Deploy rejected: {msg.get('deploy_id')} "
                f"reason: {msg.get('reason', 'unknown')}"
            )
        elif msg_type == "service_command":
            await self._handle_service_command(msg)
        else:
            logger.debug(f"Unknown orchestrator message type: {msg_type}")

    async def _handle_deploy_approval(self, msg: dict) -> None:
        """Handle a deploy_approval message from orch-server.

        Parses deploy_id, runs the registered handler in a worker thread
        (to avoid blocking the event loop during git pull + service restart),
        then sends DeployResult with success/failed status.

        For self_repo deploys, the handler returns "deferred" after scheduling
        the actual restart elsewhere; in that case DeployResult is sent on the
        NEXT startup via the orch_pending_deploy marker, not from here.
        """
        deploy_id = msg.get("deploy_id", "")
        approved_by = msg.get("approved_by", "unknown")
        logger.info(f"Deploy approved: {deploy_id} by {approved_by}")

        parsed = self._parse_deploy_id(deploy_id)
        if parsed is None:
            await self._send_deploy_result(
                deploy_id, "failed",
                error=f"invalid deploy_id format: {deploy_id!r}",
            )
            return
        node_id_in_id, repo, branch, _commit = parsed
        if node_id_in_id != self._config.node_id:
            await self._send_deploy_result(
                deploy_id, "failed",
                error=(
                    f"deploy_id node mismatch: "
                    f"{node_id_in_id} != {self._config.node_id}"
                ),
            )
            return

        if self._deploy_approval_handler is None:
            await self._send_deploy_result(
                deploy_id, "failed",
                error="no deploy_approval handler registered",
            )
            return

        started = time.monotonic()
        try:
            # Run the sync handler off the event loop. The handler returns
            # quickly for self_repo (deferred stop) and may take seconds-
            # to-minutes for other repos (git pull + restart).
            result = await asyncio.to_thread(
                self._deploy_approval_handler, deploy_id, repo, branch
            )
        except Exception as e:
            duration_ms = int((time.monotonic() - started) * 1000)
            logger.warning(f"Deploy {deploy_id} failed: {e}")
            await self._send_deploy_result(
                deploy_id, "failed", error=str(e), duration_ms=duration_ms,
            )
            return

        if result == "deferred":
            logger.info(
                f"Deploy {deploy_id} deferred (self_repo) — "
                f"DeployResult will be sent after restart"
            )
            return

        duration_ms = int((time.monotonic() - started) * 1000)
        await self._send_deploy_result(
            deploy_id, "success", duration_ms=duration_ms,
        )

    async def _send_deploy_result(
        self,
        deploy_id: str,
        status: str,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Send a single DeployResult message to orch-server."""
        result = {
            "type": "deploy_result",
            "deploy_id": deploy_id,
            "node_id": self._config.node_id,
            "status": status,
            "error": error,
            "duration_ms": duration_ms,
        }
        try:
            await self._send_json(result)
        except Exception as e:
            logger.warning(
                f"Failed to send deploy result {deploy_id}: {e}"
            )

    @staticmethod
    def _parse_deploy_id(
        deploy_id: str,
    ) -> tuple[str, str, str, str] | None:
        """Parse '{node_id}:{repo}:{branch}:{first_commit_hash}'.

        Returns (node_id, repo, branch, first_commit_hash) or None on
        format error. Uses split(':', 3) so the 4th element captures any
        leftover ':' (commit hashes are pure hex so the 4th element is
        always a single hash).
        """
        if not isinstance(deploy_id, str) or not deploy_id:
            return None
        parts = deploy_id.split(":", 3)
        if len(parts) != 4:
            return None
        return tuple(parts)  # type: ignore[return-value]

    async def _handle_service_command(self, msg: dict) -> None:
        """Handle a service_command message: execute and send result back."""
        command_id = msg.get("command_id", "")
        service_name = msg.get("service_name", "")
        action = msg.get("action", "")
        logger.info(f"Service command: {action} {service_name} (cmd={command_id})")

        success = True
        error = None
        if self._service_command_handler:
            try:
                self._service_command_handler(service_name, action)
            except Exception as e:
                success = False
                error = str(e)
        else:
            success = False
            error = "no handler registered"

        result = {
            "type": "service_command_result",
            "command_id": command_id,
            "node_id": self._config.node_id,
            "service_name": service_name,
            "action": action,
            "success": success,
            "error": error,
        }
        await self._send_json(result)

    async def _send_json(self, data: dict) -> None:
        """Send a JSON message to the orchestrator."""
        if self._ws:
            await self._ws.send(json.dumps(data))

    async def _send_heartbeat(self) -> None:
        """Send a heartbeat message with current service state."""
        msg = {
            "type": "node_status",
            "node_id": self._config.node_id,
            "services": self._get_services_info() if self._get_services_info else None,
        }
        await self._send_json(msg)

    def _reset_backoff(self) -> None:
        """Reset reconnection delay to base value."""
        self._reconnect_delay = self._config.reconnect_base

    def _next_backoff(self) -> float:
        """Get current delay and advance to next exponential step."""
        delay = self._reconnect_delay
        self._reconnect_delay = min(
            self._reconnect_delay * 2,
            self._config.reconnect_max,
        )
        return delay
