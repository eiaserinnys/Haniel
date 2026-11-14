"""Server authority for generation-bound deploy planning and finalization.

500줄을 넘는다(모듈 크기 제한 예외): PR 1 때부터 넘었고 PR 2a는 점검 필요만 더했다. 믿음과 새 글을 더하는 PR 2b 전에 나누면 같은 자리를 두 번 옮기게 되어 뒤로 미룬다.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Literal
from uuid import uuid4

from .connection_lifecycle import (
    ActiveConnection,
    ConnectionState,
)
from .deploy_attempt_connection_lifecycle import DeployAttemptConnectionLifecycle
from .deploy_attempt_deadlines import DeployAttemptDeadlines
from .deploy_attempt_progress import DeployAttemptProgress
from .event_store import EventStore
from .protocol import (
    AcceptedDeployAttemptAck,
    DeployApproval,
    DeployAttemptTerminal,
    DeployPlanProbe,
    DeployPlanProposal,
    DeployResult,
    ManifestRecoveryEvidence,
    RejectedDeployAttemptAck,
    RepoReconciliation,
)

Send = Callable[[str, str, object], Awaitable[bool]]
Broadcast = Callable[[dict], Awaitable[None]]
TerminalCallback = Callable[[dict], Awaitable[None]]
ConnectionPresence = Callable[[str, str, str], bool]


class PlanRejected(RuntimeError):
    """Manual retry preflight ended before begin."""

    def __init__(
        self,
        message: str,
        status_code: int,
        *,
        blocking_self_update: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.blocking_self_update = blocking_self_update
        super().__init__(message)


class DeployAttemptCoordinator(
    DeployAttemptConnectionLifecycle,
    DeployAttemptDeadlines,
    DeployAttemptProgress,
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

    async def resolve_terminal_canonicals(
        self,
        deploy_ids: set[str],
        *,
        message: str = "canonical became terminal during preflight",
    ) -> None:
        """Release manual waiters after the store terminalizes their probes."""
        for probe_id in list(self._probe_context):
            probe = await self.attempts.get_probe(probe_id)
            if (
                probe is not None
                and probe["deploy_id"] in deploy_ids
                and probe["status"] == "terminal"
            ):
                self._resolve_probe(probe_id, PlanRejected(message, 409))

    async def cancel_superseded_attempts(
        self, deploy_id: str, orchestrator_attempt_ids: list[str]
    ) -> None:
        """Release timers and preflight waiters superseded by node-owned success."""
        await self.resolve_terminal_canonicals(
            {deploy_id}, message="canonical succeeded through node-owned deployment"
        )
        for orchestrator_attempt_id in orchestrator_attempt_ids:
            self._cancel_timer(f"attempt:{orchestrator_attempt_id}")

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

    async def handle_result(self, msg: DeployResult) -> None:
        result = await self.attempts.record_result(
            deploy_id=msg.deploy_id,
            orchestrator_attempt_id=msg.orchestrator_attempt_id,
            connection_generation=msg.connection_generation,
            status=msg.status,
            error=msg.error,
            duration_ms=msg.duration_ms,
        )
        if result.get("status") == "ignored":
            await self._handle_ignored_report(
                node_id=msg.node_id,
                deploy_id=msg.deploy_id,
                orchestrator_attempt_id=msg.orchestrator_attempt_id,
                connection_generation=msg.connection_generation,
                report_type="deploy_result",
                summary=(
                    f"status={msg.status} duration_ms={msg.duration_ms} "
                    f"error={msg.error!r}"
                ),
            )
            return
        await self._after_store_terminal(
            msg.node_id, msg.orchestrator_attempt_id, result
        )

    async def handle_settled(self, msg: RepoReconciliation) -> None:
        if msg.orchestrator_attempt_id is None or msg.connection_generation is None:
            return
        result = await self.attempts.record_settled(
            deploy_id=msg.deploy_id,
            orchestrator_attempt_id=msg.orchestrator_attempt_id,
            connection_generation=msg.connection_generation,
            local_head=msg.local_head,
            remote_head=msg.remote_head,
        )
        if result.get("status") == "ignored":
            await self._handle_ignored_report(
                node_id=msg.node_id,
                deploy_id=msg.deploy_id,
                orchestrator_attempt_id=msg.orchestrator_attempt_id,
                connection_generation=msg.connection_generation,
                report_type="repo_reconciliation",
                summary=f"local_head={msg.local_head} remote_head={msg.remote_head}",
            )
            return
        await self._after_store_terminal(
            msg.node_id, msg.orchestrator_attempt_id, result
        )

    async def handle_evidence(self, evidence: ManifestRecoveryEvidence) -> None:
        result = await self.attempts.record_recovery_evidence(evidence)
        await self._after_store_terminal(
            evidence.node_id, evidence.orchestrator_attempt_id, result
        )

    async def handle_terminal(self, terminal: DeployAttemptTerminal) -> None:
        result = await self.attempts.record_node_terminal(terminal)
        if result.get("status") == "ignored":
            return
        await self._after_store_terminal(
            result["node_id"],
            terminal.orchestrator_attempt_id,
            result,
        )

    async def _after_store_terminal(
        self, node_id: str, orchestrator_attempt_id: str, result: dict
    ) -> None:
        if result.get("status") not in {"success", "failed", "superseded"}:
            return
        await self.resolve_terminal_canonicals(
            {result["deploy_id"]},
            message="attempt lineage changed during preflight",
        )
        self._cancel_timer(f"attempt:{orchestrator_attempt_id}")
        status = (
            "success"
            if result["status"] == "success"
            else ("rejected" if result["status"] == "superseded" else "pending")
        )
        await self._broadcast(
            {
                "type": "status_change",
                "deploy_id": result["deploy_id"],
                "status": status,
                "node_id": node_id,
            }
        )
        if self._terminal_callback is not None:
            await self._terminal_callback(
                {
                    **result,
                    "node_id": node_id,
                    "orchestrator_attempt_id": orchestrator_attempt_id,
                }
            )

    async def _requested_id(self, probe_id: str) -> str | None:
        for probe in await self.attempts.get_active_probes():
            if probe["probe_id"] == probe_id:
                return probe["requested_orchestrator_attempt_id"]
        return None

    def _probe_message(self, probe: dict) -> DeployPlanProbe:
        return DeployPlanProbe(
            probe_id=probe["probe_id"],
            connection_generation=probe["connection_generation"],
            deploy_id=probe["deploy_id"],
            node_id=probe["node_id"],
            repo=probe["repo"],
            branch=probe["branch"],
            target_head=probe["target_head"],
            source_orchestrator_attempt_id=probe["source_orchestrator_attempt_id"],
            retry_lineage=list(__import__("json").loads(probe["retry_lineage_json"])),
            expected_manifest_identity=probe["expected_manifest_identity"],
            expected_manifest_digest=probe["expected_manifest_digest"],
            deadline_at=probe["deadline_at"],
            requested_orchestrator_attempt_id=probe[
                "requested_orchestrator_attempt_id"
            ],
        )

    async def _close_unsent_attempt(
        self, node_id: str, orchestrator_attempt_id: str
    ) -> None:
        """허가가 노드에 닿지 못하면 시도를 그 자리에서 실패로 닫는다(어긋남 9)."""
        result = await self.attempts.fail_attempt_not_sent(orchestrator_attempt_id)
        await self._after_store_terminal(node_id, orchestrator_attempt_id, result)

    async def _send_auto_rejection(
        self,
        node_id: str,
        deploy_id: str,
        requested: str,
        generation: str,
        error: Literal["retry_requires_manual_approval", "self_update_pending"],
    ) -> None:
        await self._send(
            node_id,
            generation,
            RejectedDeployAttemptAck(
                accepted=False,
                requested_orchestrator_attempt_id=requested,
                deploy_id=deploy_id,
                connection_generation=generation,
                error=error,
            ),
        )

    async def _broadcast_status(self, event: dict, status: str) -> None:
        await self._broadcast(
            {
                "type": "status_change",
                "deploy_id": event["deploy_id"],
                "status": status,
                "node_id": event["node_id"],
            }
        )

    def _resolve_probe(self, probe_id: str, error: Exception) -> None:
        context = self._probe_context.pop(probe_id, None)
        self._cancel_timer(f"probe:{probe_id}")
        if context is not None and context[2] is not None and not context[2].done():
            context[2].set_exception(error)

    def _require_generation(self, node_id: str) -> str:
        state = self._connections.get(node_id)
        if not (
            isinstance(state, ActiveConnection)
            and self._connection_present(
                node_id, state.generation, state.connection_token
            )
        ):
            raise PlanRejected("node not connected", 503)
        return state.generation

    @staticmethod
    def _plan_rejected(message: str, status_code: int) -> PlanRejected:
        return PlanRejected(message, status_code)
