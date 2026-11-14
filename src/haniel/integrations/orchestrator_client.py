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
from uuid import uuid4
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from .deploy_progress import DeployProgressEmitter, ProgressCallback
from .deploy_execution_sequencer import DeployExecutionSequencer
from .deploy_reporting import (
    DeployReporter,
    deploy_result_message,
    parse_deploy_id,
    reconciliation_message,
    settled_result_messages,
)
from .deploy_attempt_gate import DeployAttemptGate
from ..core.thread_shutdown import join_thread_with_timeout

if TYPE_CHECKING:
    from ..config.model import OrchestratorClientConfig
    from ..core.repo_reconciliation import RepoReconciliationSnapshot

logger = logging.getLogger(__name__)

DEPLOY_PLAN_REASON_INVENTORY = frozenset(
    {
        "legacy_retry",
        "manifest_read_failed",
        "manifest_mismatch",
        "normal_pull",
        "journal_missing",
        "journal_target_mismatch",
        "journal_scope_mismatch",
        "journal_manifest_mismatch",
        "journal_link_missing",
        "retry_lineage_mismatch",
        "journal_completion_missing",
        "durable_local_success",
        "manifest_retry",
        "unsafe_journal",
        "planner_missing",
        "planner_error",
    }
)


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
        service_command_handler: (
            "Callable[[str, str, dict[str, Any] | None], Any] | None"
        ) = None,
        deploy_approval_handler: (
            "Callable[[dict[str, Any], ProgressCallback], "
            "str | dict[str, Any] | None] | None"
        ) = None,
        deploy_plan_probe_handler: "Callable[[dict[str, Any]], dict[str, Any]] | None" = None,
        repo_snapshot_handler: (
            "Callable[[str, str, str], RepoReconciliationSnapshot] | None"
        ) = None,
        expected_budget_handler: "Callable[[str], int | None] | None" = None,
    ) -> None:
        self._config = config
        self._haniel_version = haniel_version
        self._get_services_info = get_services_info
        self._service_command_handler = service_command_handler
        # handler convention: immutable approval dict -> result.
        #   None       — synchronous success; we send DeployResult{success}.
        #   "deferred" — handler scheduled the work elsewhere (e.g. self-update
        #                via deferred stop); DeployResult is sent on next
        #                startup via enqueue_deploy_result.
        #   Exception  — failure; we send DeployResult{failed, error=str(e)}.
        self._deploy_approval_handler = deploy_approval_handler
        self._deploy_plan_probe_handler = deploy_plan_probe_handler
        self._repo_snapshot_handler = repo_snapshot_handler
        self._expected_budget_handler = expected_budget_handler
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
        self._flush_lock: asyncio.Lock | None = None
        self._flush_lock_loop: asyncio.AbstractEventLoop | None = None
        self._deploy_attempt_gate = DeployAttemptGate()
        self._deploy_reporter = DeployReporter(
            node_id=config.node_id,
            approval_handler=deploy_approval_handler,
            snapshot_handler=repo_snapshot_handler,
            expected_budget_handler=expected_budget_handler,
            send_json=lambda message: self._send_json(message),
            deliver=lambda messages: self._deliver_deploy_messages(messages),
        )
        # Approvals execute one at a time, in arrival order, for the client's
        # whole lifetime; the receive loop only lines them up.
        self._sequencer = DeployExecutionSequencer(self._handle_deploy_approval)
        # Plan probes and service commands run as loop tasks outside _listen.
        self._background_tasks: set[asyncio.Task[None]] = set()
        # Counts sockets; a proposal is only sent on the socket that asked.
        self._connection_serial = 0

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
        loop = self._loop
        ws = self._ws
        if loop is not None and ws is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except Exception as exc:
                logger.debug("Failed to close orchestrator connection: %s", exc)
        join_thread_with_timeout(
            self._thread,
            name="orchestrator client thread",
        )

    def notify_change(
        self,
        repo: str,
        branch: str,
        commits: list[str],
        affected_services: list[str],
        diff_stat: str | None = None,
        deploy_id: str | None = None,
        target_head: str | None = None,
        deployment_kind: str = "legacy",
        expected_manifest_identity: str | None = None,
        expected_manifest_digest: str | None = None,
        is_self_update: bool = False,
        wait: bool = False,
    ) -> None:
        """Notify the orchestrator of detected changes. Thread-safe.

        If not connected, the notification is silently dropped
        (graceful degradation — the node operates independently).
        """
        if not self._connected or not self._ws or not self._loop:
            return

        if not commits:
            return  # Nothing to notify

        if deploy_id is None:
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
            "target_head": target_head,
            "deployment_kind": deployment_kind,
            "expected_manifest_identity": expected_manifest_identity,
            "expected_manifest_digest": expected_manifest_digest,
            "is_self_update": is_self_update,
        }

        try:
            future = asyncio.run_coroutine_threadsafe(self._send_json(msg), self._loop)
            if wait:
                future.result(timeout=self._config.ping_timeout)
        except Exception as e:
            logger.debug(f"Failed to queue change notification: {e}")

    def notify_repo_reconciliation(
        self,
        snapshot: "RepoReconciliationSnapshot",
        phase: str,
        *,
        orchestrator_attempt_id: str | None = None,
        wait: bool = False,
    ) -> None:
        """Send one explicit HEAD reconciliation message from a sync runner thread."""
        self._schedule_report(
            lambda: self._deploy_reporter.send_reconciliation(
                snapshot, phase, orchestrator_attempt_id=orchestrator_attempt_id
            ),
            wait=wait,
        )

    def report_deploy_attempt(
        self,
        snapshot: "RepoReconciliationSnapshot",
        operation_error: str | None,
        duration_ms: int,
        permit: dict[str, Any],
    ) -> None:
        """Queue DeployResult then settled reconciliation. Thread-safe.

        The pair goes through the ordered reconnect buffer, so a report that
        finishes while the socket is down leaves on the next connection.
        """
        self._enqueue_buffered_deploy_messages(
            settled_result_messages(
                self._config.node_id,
                snapshot,
                operation_error,
                duration_ms,
                permit["begun_orchestrator_attempt_id"],
                permit["connection_generation"],
            )
        )

    def start_deploy_progress(self, permit: dict[str, Any]) -> DeployProgressEmitter:
        """Start the bounded-lifetime heartbeat source for an auto attempt."""

        def emit(message: dict) -> None:
            self._schedule_report(
                lambda message=message: self._send_json(message), wait=False
            )

        expected_budget_sec = None
        if self._expected_budget_handler is not None:
            try:
                expected_budget_sec = self._expected_budget_handler(permit["deploy_id"])
            except Exception as exc:
                logger.warning(
                    "Failed to calculate expected budget for %s: %s",
                    permit["deploy_id"],
                    exc,
                )

        emitter = DeployProgressEmitter(
            node_id=self._config.node_id,
            deploy_id=permit["deploy_id"],
            orchestrator_attempt_id=permit["begun_orchestrator_attempt_id"],
            connection_generation=permit["connection_generation"],
            emit=emit,
            expected_budget_sec=expected_budget_sec,
        )
        emitter.start()
        return emitter

    def request_auto_attempt(
        self, snapshot: "RepoReconciliationSnapshot"
    ) -> dict[str, Any]:
        """Request and wait for an explicit generation-bound execution ACK."""
        requested = uuid4().hex
        self._deploy_attempt_gate.register(requested, snapshot.deploy_id)
        self.notify_repo_reconciliation(
            snapshot,
            "attempt_started",
            orchestrator_attempt_id=requested,
            wait=True,
        )
        return self._deploy_attempt_gate.wait(requested, self._config.ping_timeout)

    def _schedule_report(
        self,
        coroutine_factory: Callable[[], Any],
        *,
        wait: bool,
    ) -> None:
        if not self._connected or not self._ws or not self._loop:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine_factory(), self._loop)
            if wait:
                future.result(timeout=self._config.ping_timeout)
        except Exception as exc:
            logger.warning("Failed to send orchestrator deploy report: %s", exc)

    async def _services_snapshot(self) -> "list[dict] | None":
        """Read config-guarded service state without parking the event loop.

        The reader blocks on a runner threading lock. Awaiting it in a worker
        thread keeps pongs, socket reads, and cross-thread coroutine scheduling
        alive while a runner thread holds that lock.
        """
        if self._get_services_info is None:
            return None
        return await asyncio.to_thread(self._get_services_info)

    def _build_node_hello(self, services: "list[dict] | None" = None) -> dict:
        """Build the NodeHello payload sent after connecting."""
        return {
            "type": "node_hello",
            "node_id": self._config.node_id,
            "token": self._config.token,
            "hostname": self._config.hostname or platform.node(),
            "os": platform.system(),
            "arch": platform.machine(),
            "haniel_version": self._haniel_version,
            "services": services,
        }

    def enqueue_deploy_result(
        self,
        deploy_id: str,
        status: str,
        error: str | None = None,
        duration_ms: int | None = None,
        orchestrator_attempt_id: str = "",
        connection_generation: str = "",
        settled_snapshot: "RepoReconciliationSnapshot | None" = None,
    ) -> None:
        """Buffer a DeployResult to send on the next successful connection.

        Used by runner.start() to report self-update results that completed
        during the previous wrapper iteration. Thread-safe.
        """
        messages = [
            deploy_result_message(
                self._config.node_id,
                deploy_id,
                orchestrator_attempt_id,
                connection_generation,
                status,
                error=error,
                duration_ms=duration_ms,
            )
        ]
        if settled_snapshot is not None:
            messages.append(
                reconciliation_message(
                    settled_snapshot,
                    "settled",
                    orchestrator_attempt_id=orchestrator_attempt_id,
                    connection_generation=connection_generation,
                )
            )
        self._enqueue_buffered_deploy_messages(messages)

    def enqueue_node_deploy_report(
        self,
        *,
        phase: str,
        node_attempt_id: str,
        journal_attempt_id: str | None,
        deploy_id: str,
        repo: str,
        branch: str,
        target_head: str,
        local_head: str,
        trigger: str,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Buffer one node-owned deploy lifecycle report across reconnects."""
        self._enqueue_buffered_deploy_messages(
            [
                {
                    "type": "node_deploy_report",
                    "phase": phase,
                    "node_attempt_id": node_attempt_id,
                    "journal_attempt_id": journal_attempt_id,
                    "deploy_id": deploy_id,
                    "node_id": self._config.node_id,
                    "repo": repo,
                    "branch": branch,
                    "target_head": target_head,
                    "local_head": local_head,
                    "trigger": trigger,
                    "error": error,
                    "duration_ms": duration_ms,
                }
            ]
        )

    def _enqueue_buffered_deploy_messages(self, messages: list[dict[str, Any]]) -> None:
        """Append messages atomically and kick the single ordered flush."""
        with self._pending_lock:
            self._pending_deploy_results.extend(messages)
        if self._connected and self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._flush_pending_deploy_results(), self._loop
                )
            except Exception as e:
                logger.debug(f"Failed to schedule deploy result flush: {e}")

    async def _deliver_deploy_messages(self, messages: list[dict[str, Any]]) -> None:
        """Send attempt-closing messages through the one ordered buffer.

        They queue behind anything already waiting; the buffer is flushed now
        when a socket is up. Whatever cannot be sent stays for the next
        connection instead of being dropped. A send that timed out after the
        bytes left may be repeated; the orchestrator answers a repeat
        with deploy_report_ack.
        """
        with self._pending_lock:
            self._pending_deploy_results.extend(messages)
        if self._connected and self._ws is not None:
            await self._flush_pending_deploy_results()

    async def _flush_pending_deploy_results(self) -> None:
        """Send the buffered prefix while preserving the complete unsent suffix."""
        async with self._flush_lock_for_running_loop():
            while True:
                with self._pending_lock:
                    if not self._pending_deploy_results:
                        return
                    msg = self._pending_deploy_results.pop(0)
                try:
                    await self._send_json(msg)
                except Exception as e:
                    logger.warning(
                        "Failed to send buffered deploy result %s: %s",
                        msg.get("deploy_id"),
                        e,
                    )
                    with self._pending_lock:
                        self._pending_deploy_results.insert(0, msg)
                    return

    def _flush_lock_for_running_loop(self) -> asyncio.Lock:
        """Return one flush lock for the client's current background event loop."""
        loop = asyncio.get_running_loop()
        if self._flush_lock is None or self._flush_lock_loop is not loop:
            self._flush_lock = asyncio.Lock()
            self._flush_lock_loop = loop
        return self._flush_lock

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
        """Main loop: connect → listen → reconnect with backoff.

        The approval sequencer and the detached answer tasks belong to this
        loop, not to one connection: they keep running across reconnects and
        stop only here, when the client stops.
        """
        self._sequencer.start()
        try:
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
        finally:
            await self._sequencer.close()
            await self._cancel_background_tasks()

    def _spawn_background(self, coroutine, name: str) -> asyncio.Task[None]:
        """Run one answer outside the receive loop, bound to the client's loop."""
        task = asyncio.get_running_loop().create_task(
            self._guard_background(coroutine, name), name=name
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    @staticmethod
    async def _guard_background(coroutine, name: str) -> None:
        try:
            await coroutine
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Orchestrator task %s failed: %s", name, exc)

    async def _cancel_background_tasks(self) -> None:
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _connect_and_listen(self) -> None:
        """Connect to orchestrator, send NodeHello, and listen for messages."""
        import websockets

        async with websockets.connect(
            self._config.url,
            open_timeout=self._config.ping_timeout,
            ping_interval=self._config.ping_interval,
            ping_timeout=self._config.ping_timeout,
            close_timeout=self._config.ping_timeout,
        ) as ws:
            self._ws = ws
            self._connection_serial += 1
            self._deploy_attempt_gate.reset_connection()

            # Send NodeHello
            await self._send_json(
                self._build_node_hello(await self._services_snapshot())
            )

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
            except asyncio.CancelledError:
                # The client itself is being cancelled: take both socket tasks
                # down with it and let the cancellation reach _run.
                pending, done = {listener, heartbeat}, set()
                raise
            finally:
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            # Re-raise if listener had an exception
            for task in done:
                task.result()

    async def _listen(self, ws) -> None:
        """Read, validate, hand off, read again; never wait for an execution."""
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
                logger.warning(f"Heartbeat send failed: {e}")
                break

    async def _handle_server_message(self, msg: dict) -> None:
        """Handle messages from the orchestrator server."""
        msg_type = msg.get("type")
        if msg_type == "deploy_approval":
            self._deploy_attempt_gate.observe_generation(msg["connection_generation"])
            self._sequencer.submit(msg)
        elif msg_type == "deploy_plan_probe":
            self._spawn_background(
                self._handle_deploy_plan_probe(msg),
                f"deploy-plan-probe:{msg.get('probe_id')}",
            )
        elif msg_type == "deploy_attempt_ack":
            self._deploy_attempt_gate.observe_generation(msg["connection_generation"])
            self._deploy_attempt_gate.accept_ack(msg)
        elif msg_type == "deploy_report_ack":
            logger.warning(
                "Orchestrator ignored deploy report attempt_id=%s report_type=%s "
                "reason=%s outcome=%s",
                msg.get("orchestrator_attempt_id"),
                msg.get("report_type"),
                msg.get("reason"),
                msg.get("attempt_outcome") or "missing",
            )
        elif msg_type == "deploy_reject":
            logger.info(
                f"Deploy rejected: {msg.get('deploy_id')} "
                f"reason: {msg.get('reason', 'unknown')}"
            )
        elif msg_type == "service_command":
            self._spawn_background(
                self._handle_service_command(msg),
                f"service-command:{msg.get('command_id')}",
            )
        else:
            logger.debug(f"Unknown orchestrator message type: {msg_type}")

    async def _handle_deploy_approval(self, msg: dict) -> None:
        """Execute one approval (called by the sequencer, one at a time)."""
        await self._deploy_reporter.handle_approval(msg)

    async def _handle_deploy_plan_probe(self, msg: dict) -> None:
        """Run the read-only planner and answer on the socket that asked.

        A probe is bound to its connection generation; once that socket is
        gone the orchestrator has already closed the probe, so the proposal
        is dropped rather than sent through a newer connection.
        """
        self._deploy_attempt_gate.observe_generation(msg["connection_generation"])
        asked_on = self._connection_serial
        if self._deploy_plan_probe_handler is None:
            proposal = {
                "type": "deploy_plan_proposal",
                "mode": "fail_closed",
                "reason": "planner_missing",
                "error": "no deploy plan probe handler registered",
                "fingerprint": "planner-missing",
                "current_head": "",
                "journal_status": None,
                "journal_attempt_id": None,
                "linked_orchestrator_attempt_id": None,
                "journal_target_head": None,
                "original_previous_head": None,
                "manifest_identity": None,
                "manifest_digest": None,
                "journal_completed_at": None,
            }
        else:
            try:
                proposal = await asyncio.to_thread(self._deploy_plan_probe_handler, msg)
            except Exception as exc:
                proposal = {
                    "type": "deploy_plan_proposal",
                    "mode": "fail_closed",
                    "reason": "planner_error",
                    "error": f"deploy plan probe failed: {exc}",
                    "fingerprint": "planner-error",
                    "current_head": "",
                    "journal_status": None,
                    "journal_attempt_id": None,
                    "linked_orchestrator_attempt_id": None,
                    "journal_target_head": None,
                    "original_previous_head": None,
                    "manifest_identity": None,
                    "manifest_digest": None,
                    "journal_completed_at": None,
                }
        if asked_on != self._connection_serial:
            logger.info(
                "Dropping deploy plan proposal probe=%s: the connection that asked "
                "is gone",
                msg["probe_id"],
            )
            return
        try:
            await self._send_json(
                {
                    **proposal,
                    "type": "deploy_plan_proposal",
                    "probe_id": msg["probe_id"],
                    "connection_generation": msg["connection_generation"],
                    "deploy_id": msg["deploy_id"],
                    "node_id": msg["node_id"],
                    "repo": msg["repo"],
                    "branch": msg["branch"],
                    "target_head": msg["target_head"],
                }
            )
        except Exception as exc:
            logger.warning(
                "Dropping deploy plan proposal probe=%s: %s", msg["probe_id"], exc
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
        return parse_deploy_id(deploy_id)

    async def _handle_service_command(self, msg: dict) -> None:
        """Execute one service command off the loop and answer on the socket
        that is up when it finishes; without one the answer is logged and
        dropped (the orchestrator has already timed the command out)."""
        command_id = msg.get("command_id", "")
        service_name = msg.get("service_name", "")
        action = msg.get("action", "")
        payload = msg.get("payload")
        logger.info(f"Service command: {action} {service_name} (cmd={command_id})")

        success = True
        error = None
        result_data = None
        if self._service_command_handler:
            try:
                result_data = await asyncio.to_thread(
                    self._service_command_handler,
                    service_name,
                    action,
                    payload if isinstance(payload, dict) else None,
                )
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
            "result": result_data,
        }
        try:
            await self._send_json(result)
        except Exception as exc:
            logger.warning(
                "Dropping service command result cmd=%s (%s %s, success=%s): "
                "no connection to answer on: %s",
                command_id,
                action,
                service_name,
                success,
                exc,
            )

    async def _send_json(self, data: dict) -> None:
        """Send a JSON message to the orchestrator."""
        if self._ws is None:
            raise ConnectionError("orchestrator websocket is not connected")
        await asyncio.wait_for(
            self._ws.send(json.dumps(data)),
            timeout=self._config.ping_timeout,
        )

    async def _send_heartbeat(self) -> None:
        """Send a heartbeat message with current service state.

        Reads service state off the loop; see _services_snapshot.
        """
        services = await self._services_snapshot()
        msg = {
            "type": "node_status",
            "node_id": self._config.node_id,
            "services": services,
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
