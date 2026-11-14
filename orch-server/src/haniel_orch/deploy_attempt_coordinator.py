"""Server authority for generation-bound deploy planning and finalization.

노드 보고 처리기(결과, 정착, 증거 회수, deploy_attempt_terminal)는 `deploy_attempt_report_handlers`에 있다.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable
from uuid import uuid4

from .connection_lifecycle import (
    ActiveConnection,
    ConnectionState,
)
from .deploy_attempt_connection_lifecycle import DeployAttemptConnectionLifecycle
from .deploy_attempt_coordinator_support import DeployAttemptCoordinatorSupport, PlanRejected
from .deploy_attempt_deadlines import DeployAttemptDeadlines
from .deploy_attempt_progress import DeployAttemptProgress
from .deploy_attempt_report_handlers import DeployAttemptReportHandlers
from .event_store import EventStore
from .protocol import (
    AcceptedDeployAttemptAck,
    DeployApproval,
    DeployPlanProposal,
    RepoReconciliation,
)

Send = Callable[[str, str, object], Awaitable[bool]]
Broadcast = Callable[[dict], Awaitable[None]]
TerminalCallback = Callable[[dict], Awaitable[None]]
ConnectionPresence = Callable[[str, str, str], bool]


class DeployAttemptCoordinator(
    DeployAttemptConnectionLifecycle,
    DeployAttemptCoordinatorSupport,
    DeployAttemptDeadlines,
    DeployAttemptProgress,
    DeployAttemptReportHandlers,
):
    """The sole authority that turns evidence into deploy state transitions."""

    def __init__(
        self,
        store: EventStore,
        send: Send,
        broadcast: Broadcast,
        connection_present: ConnectionPresence,
        *,
        probe_timeout_sec: float,
        attempt_timeout_sec: float,
        terminal_callback: TerminalCallback | None = None,
    ) -> None:
        self._store = store
        self._send = send
        self._broadcast = broadcast
        self._connection_present = connection_present
        self._probe_timeout_sec = probe_timeout_sec
        self._attempt_timeout_sec = attempt_timeout_sec
        self._terminal_callback = terminal_callback
        self._coordination_lock = asyncio.Lock()
        self._connections: dict[str, ConnectionState] = {}
        self._probe_context: dict[str, tuple[str, str, asyncio.Future[str] | None]] = {}
        self._timers: dict[str, asyncio.Task[None]] = {}

    @property
    def attempts(self):
        attempts = self._store.attempts
        if attempts is None:
            raise RuntimeError("EventStore is not initialized")
        return attempts

    async def restore_deadlines(self) -> None:
        """Close pre-restart probes and recreate persisted attempt deadlines."""
        for probe in await self.attempts.get_active_probes():
            await self.attempts.terminalize_preflight(
                probe["probe_id"],
                kind="preflight_disconnected",
                stage="startup",
                reason="server_restarted",
                error="probe connection generation cannot survive server restart",
            )
        for attempt in await self.attempts.get_active_attempts():
            self._schedule_attempt(
                attempt["orchestrator_attempt_id"], attempt["deadline_at"]
            )

    async def approve_manual(
        self, event: dict, *, approved_by: str, source: str
    ) -> str:
        async with self._coordination_lock:
            blocking_self_update = await self._store.get_active_self_update_for_node(
                event["node_id"]
            )
            if not event.get("is_self_update") and blocking_self_update is not None:
                raise PlanRejected(
                    f"self-update {blocking_self_update['deploy_id']} must be approved "
                    f"or postponed before other deployments on node {event['node_id']}",
                    409,
                    blocking_self_update={
                        key: blocking_self_update[key]
                        for key in (
                            "deploy_id",
                            "node_id",
                            "repo",
                            "branch",
                            "target_head",
                            "status",
                            "created_at",
                            "updated_at",
                        )
                    },
                )
            generation = self._require_generation(event["node_id"])
            # 실행했을 수 있는 시도(source)가 있는 표시 줄만 계획부터 묻는다. source가 빈 표시 줄은 첫 승인 길이다
            retry = await self.attempts.get_retry_requirement(event["deploy_id"])
            retry_required = (
                retry is not None and retry["source_orchestrator_attempt_id"] is not None
            )
            if not retry_required:
                orchestrator_attempt_id = uuid4().hex
                deadline = self._deadline(self._attempt_timeout_sec)
                try:
                    await self.attempts.begin_normal_attempt(
                        orchestrator_attempt_id=orchestrator_attempt_id,
                        deploy_id=event["deploy_id"],
                        connection_generation=generation,
                        current_generation=generation,
                        source=source,
                        approved_by=approved_by,
                        deadline_at=deadline,
                    )
                except ValueError as exc:
                    if str(exc) == "retry requires preflight":
                        retry_required = True
                    else:
                        raise PlanRejected(str(exc), 409) from exc
                except PermissionError as exc:
                    raise PlanRejected(str(exc), 409) from exc
                if not retry_required:
                    approval = DeployApproval(
                        deploy_id=event["deploy_id"],
                        orchestrator_attempt_id=orchestrator_attempt_id,
                        execution_mode="execute",
                        connection_generation=generation,
                        approved_by=approved_by,
                    )
                    sent = await self._send(event["node_id"], generation, approval)
                    if not sent:
                        await self._close_unsent_attempt(
                            event["node_id"], orchestrator_attempt_id
                        )
                        raise PlanRejected("node not connected", 503)
                    self._schedule_attempt(orchestrator_attempt_id, deadline)
                    await self._broadcast_status(event, "deploying")
                    return orchestrator_attempt_id

            probe_id = uuid4().hex
            future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            self._probe_context[probe_id] = (source, approved_by, future)
            try:
                probe = await self.attempts.create_probe(
                    probe_id=probe_id,
                    deploy_id=event["deploy_id"],
                    connection_generation=generation,
                    deadline_at=self._deadline(self._probe_timeout_sec),
                    manual_retry=True,
                )
            except (ValueError, PermissionError) as exc:
                self._probe_context.pop(probe_id, None)
                raise PlanRejected(str(exc), 409) from exc
            if not await self._send(
                event["node_id"], generation, self._probe_message(probe)
            ):
                await self.attempts.terminalize_preflight(
                    probe_id,
                    kind="preflight_disconnected",
                    stage="probe_send",
                    reason="node_disconnected",
                    error="node disconnected before plan probe delivery",
                )
                self._probe_context.pop(probe_id, None)
                raise PlanRejected("node not connected", 503)
            self._schedule_probe(probe_id)
        return await future

    async def handle_auto_request(self, msg: RepoReconciliation) -> None:
        requested = msg.orchestrator_attempt_id
        if requested is None:
            raise ValueError("auto attempt request requires orchestrator_attempt_id")
        async with self._coordination_lock:
            generation = self._require_generation(msg.node_id)
            event = await self._store.get_deploy_event(msg.deploy_id)
            if event is None or not event.get("is_self_update"):
                active_self_update = await self._store.get_active_self_update_for_node(
                    msg.node_id
                )
                if active_self_update is not None:
                    await self._send_auto_rejection(
                        msg.node_id,
                        msg.deploy_id,
                        requested,
                        generation,
                        "self_update_pending",
                    )
                    return
            if await self.attempts.inspection_required(msg.deploy_id):
                await self._send_auto_rejection(
                    msg.node_id,
                    msg.deploy_id,
                    requested,
                    generation,
                    "retry_requires_manual_approval",
                )
                return
            probe_id = uuid4().hex
            try:
                probe = await self.attempts.create_probe(
                    probe_id=probe_id,
                    deploy_id=msg.deploy_id,
                    connection_generation=generation,
                    deadline_at=self._deadline(self._probe_timeout_sec),
                    manual_retry=False,
                    requested_orchestrator_attempt_id=requested,
                )
            except PermissionError as exc:
                if str(exc) == "retry_requires_manual_approval":
                    await self._send_auto_rejection(
                        msg.node_id,
                        msg.deploy_id,
                        requested,
                        generation,
                        "retry_requires_manual_approval",
                    )
                return
            except ValueError:
                return
            self._probe_context[probe_id] = ("auto", "system:auto", None)
            if not await self._send(
                msg.node_id, generation, self._probe_message(probe)
            ):
                await self.attempts.terminalize_preflight(
                    probe_id,
                    kind="preflight_disconnected",
                    stage="probe_send",
                    reason="node_disconnected",
                    error="node disconnected before auto plan probe delivery",
                )
                return
            self._schedule_probe(probe_id)

    async def handle_proposal(
        self, proposal: DeployPlanProposal, *, connection_token: str
    ) -> None:
        async with self._coordination_lock:
            context = self._probe_context.get(proposal.probe_id)
            if context is None:
                return
            source, approved_by, future = context
            probe = await self.attempts.get_probe(proposal.probe_id)
            state = self._connections.get(proposal.node_id)
            if probe is None:
                return
            coordinator_generation_matches = (
                isinstance(state, ActiveConnection)
                and proposal.connection_generation == probe["connection_generation"]
                and probe["connection_generation"] == state.generation
                and connection_token == state.connection_token
                and self._connection_present(
                    proposal.node_id, state.generation, state.connection_token
                )
            )
            current_generation = (
                state.generation if coordinator_generation_matches else ""
            )
            probe_requested = source == "auto"
            orchestrator_attempt_id = (
                probe["requested_orchestrator_attempt_id"]
                if probe_requested
                else uuid4().hex
            )
            if orchestrator_attempt_id is None:
                return
            deadline = self._deadline(self._attempt_timeout_sec)
            result = await self.attempts.record_proposal_and_begin(
                proposal,
                orchestrator_attempt_id=orchestrator_attempt_id,
                source=source,
                approved_by=approved_by,
                deadline_at=deadline,
                current_generation=current_generation,
            )
            if not coordinator_generation_matches and result["status"] == "begun":
                raise RuntimeError("store began an attempt for a stale generation")
            if result["status"] == "duplicate_begun":
                return
            if result["status"] != "begun":
                self._resolve_probe(
                    proposal.probe_id,
                    PlanRejected(result.get("kind", "preflight rejected"), 422),
                )
                if (
                    source == "auto"
                    and result.get("reason") == "retry_requires_manual_approval"
                ):
                    # 탐침을 만든 뒤 점검 필요가 섰다. 노드가 시간 초과까지 기다리지 않게 거절 답을 보낸다
                    await self._send_auto_rejection(
                        probe["node_id"],
                        probe["deploy_id"],
                        probe["requested_orchestrator_attempt_id"],
                        probe["connection_generation"],
                        "retry_requires_manual_approval",
                    )
                return
            attempt = result["attempt"]
            if source == "auto":
                permit = AcceptedDeployAttemptAck(
                    accepted=True,
                    requested_orchestrator_attempt_id=orchestrator_attempt_id,
                    begun_orchestrator_attempt_id=orchestrator_attempt_id,
                    deploy_id=attempt["deploy_id"],
                    connection_generation=attempt["connection_generation"],
                    probe_id=attempt["probe_id"],
                    execution_mode=attempt["execution_mode"],
                    preflight_fingerprint=attempt["proposal_fingerprint"],
                )
            else:
                permit = DeployApproval(
                    deploy_id=attempt["deploy_id"],
                    orchestrator_attempt_id=orchestrator_attempt_id,
                    execution_mode=attempt["execution_mode"],
                    probe_id=attempt["probe_id"],
                    connection_generation=attempt["connection_generation"],
                    preflight_fingerprint=attempt["proposal_fingerprint"],
                    approved_by=approved_by,
                )
            sent = await self._send(
                attempt["node_id"], attempt["connection_generation"], permit
            )
            self._cancel_timer(f"probe:{proposal.probe_id}")
            self._probe_context.pop(proposal.probe_id, None)
            if sent:
                self._schedule_attempt(orchestrator_attempt_id, deadline)
                await self._broadcast_status(attempt, "deploying")
            else:
                await self._close_unsent_attempt(
                    attempt["node_id"], orchestrator_attempt_id
                )
            if future is not None and not future.done():
                if sent:
                    future.set_result(orchestrator_attempt_id)
                else:
                    future.set_exception(PlanRejected("approval delivery failed", 503))

