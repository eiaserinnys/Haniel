"""Server authority for generation-bound deploy planning and finalization."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable
from uuid import uuid4

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

Send = Callable[[str, object], Awaitable[bool]]
Broadcast = Callable[[dict], Awaitable[None]]
TerminalCallback = Callable[[dict], Awaitable[None]]


class PlanRejected(RuntimeError):
    """Manual retry preflight ended before begin."""

    def __init__(self, message: str, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(message)


class DeployAttemptCoordinator:
    """The sole authority that turns evidence into deploy state transitions."""

    def __init__(
        self,
        store: EventStore,
        send: Send,
        broadcast: Broadcast,
        *,
        probe_timeout_sec: float,
        attempt_timeout_sec: float,
        terminal_callback: TerminalCallback | None = None,
    ) -> None:
        self._store = store
        self._send = send
        self._broadcast = broadcast
        self._probe_timeout_sec = probe_timeout_sec
        self._attempt_timeout_sec = attempt_timeout_sec
        self._terminal_callback = terminal_callback
        self._generations: dict[str, str] = {}
        self._probe_context: dict[str, tuple[str, str, asyncio.Future[str] | None]] = {}
        self._timers: dict[str, asyncio.Task[None]] = {}

    @property
    def attempts(self):
        attempts = self._store.attempts
        if attempts is None:
            raise RuntimeError("EventStore is not initialized")
        return attempts

    def register_connection(self, node_id: str) -> str:
        generation = uuid4().hex
        self._generations[node_id] = generation
        return generation

    def current_generation(self, node_id: str) -> str | None:
        return self._generations.get(node_id)

    async def disconnect(self, node_id: str, generation: str) -> None:
        if self._generations.get(node_id) == generation:
            self._generations.pop(node_id, None)
        for probe_id in await self.attempts.terminalize_generation(generation):
            self._resolve_probe(probe_id, PlanRejected("node disconnected", 503))

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
            self._schedule_attempt(attempt["orchestrator_attempt_id"], attempt["deadline_at"])

    async def shutdown(self) -> None:
        """Stop only in-memory timers; durable state remains for startup recovery."""
        timers = list(self._timers.values())
        self._timers.clear()
        for timer in timers:
            timer.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)

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
                self._resolve_probe(
                    probe_id, PlanRejected(message, 409)
                )

    async def approve_manual(
        self, event: dict, *, approved_by: str, source: str
    ) -> str:
        generation = self._require_generation(event["node_id"])
        if not await self.attempts.has_retry_requirement(event["deploy_id"]):
            orchestrator_attempt_id = uuid4().hex
            deadline = self._deadline(self._attempt_timeout_sec)
            try:
                await self.attempts.begin_normal_attempt(
                    orchestrator_attempt_id=orchestrator_attempt_id,
                    deploy_id=event["deploy_id"],
                    connection_generation=generation,
                    source=source,
                    approved_by=approved_by,
                    deadline_at=deadline,
                )
            except PermissionError as exc:
                raise PlanRejected(str(exc), 409) from exc
            approval = DeployApproval(
                deploy_id=event["deploy_id"],
                orchestrator_attempt_id=orchestrator_attempt_id,
                execution_mode="execute",
                connection_generation=generation,
                approved_by=approved_by,
            )
            if not await self._send(event["node_id"], approval):
                # The committed attempt remains owned by its durable deadline.
                # Retrying or rolling it back here would create a second terminal
                # authority beside the persisted attempt timeout.
                self._schedule_attempt(orchestrator_attempt_id, deadline)
                raise PlanRejected("node not connected", 503)
            self._schedule_attempt(orchestrator_attempt_id, deadline)
            await self._broadcast_status(event, "deploying")
            return orchestrator_attempt_id

        probe_id = uuid4().hex
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._probe_context[probe_id] = (source, approved_by, future)
        probe = await self.attempts.create_probe(
            probe_id=probe_id,
            deploy_id=event["deploy_id"],
            connection_generation=generation,
            deadline_at=self._deadline(self._probe_timeout_sec),
            manual_retry=True,
        )
        if not await self._send(event["node_id"], self._probe_message(probe)):
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
        generation = self._require_generation(msg.node_id)
        if await self.attempts.has_retry_requirement(msg.deploy_id):
            await self._send_auto_retry_rejection(msg, requested, generation)
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
                await self._send_auto_retry_rejection(msg, requested, generation)
            return
        except ValueError:
            return
        self._probe_context[probe_id] = ("auto", "system:auto", None)
        if not await self._send(msg.node_id, self._probe_message(probe)):
            await self.attempts.terminalize_preflight(
                probe_id,
                kind="preflight_disconnected",
                stage="probe_send",
                reason="node_disconnected",
                error="node disconnected before auto plan probe delivery",
            )
            return
        self._schedule_probe(probe_id)

    async def handle_proposal(self, proposal: DeployPlanProposal) -> None:
        context = self._probe_context.get(proposal.probe_id)
        if context is None:
            return
        source, approved_by, future = context
        probe_requested = source == "auto"
        orchestrator_attempt_id = (
            await self._requested_id(proposal.probe_id)
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
        )
        if result["status"] == "duplicate_begun":
            return
        if result["status"] != "begun":
            self._cancel_timer(f"probe:{proposal.probe_id}")
            self._resolve_probe(
                proposal.probe_id,
                PlanRejected(result.get("kind", "preflight rejected"), 422),
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
        sent = await self._send(attempt["node_id"], permit)
        self._cancel_timer(f"probe:{proposal.probe_id}")
        self._probe_context.pop(proposal.probe_id, None)
        self._schedule_attempt(orchestrator_attempt_id, deadline)
        await self._broadcast_status(attempt, "deploying")
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
        await self._after_store_terminal(msg.node_id, msg.orchestrator_attempt_id, result)

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
        await self._after_store_terminal(msg.node_id, msg.orchestrator_attempt_id, result)

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
        self._cancel_timer(f"attempt:{orchestrator_attempt_id}")
        status = "success" if result["status"] == "success" else (
            "rejected" if result["status"] == "superseded" else "pending"
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

    def _schedule_probe(self, probe_id: str) -> None:
        self._timers[f"probe:{probe_id}"] = asyncio.create_task(
            self._probe_timeout(probe_id)
        )

    def _schedule_attempt(self, orchestrator_attempt_id: str, deadline_at: str) -> None:
        delay = max(
            0.0,
            (datetime.fromisoformat(deadline_at) - datetime.now(timezone.utc)).total_seconds(),
        )
        self._timers[f"attempt:{orchestrator_attempt_id}"] = asyncio.create_task(
            self._attempt_timeout(orchestrator_attempt_id, delay)
        )

    async def _probe_timeout(self, probe_id: str) -> None:
        try:
            await asyncio.sleep(self._probe_timeout_sec)
        except asyncio.CancelledError:
            return
        if await self.attempts.terminalize_preflight(
            probe_id,
            kind="preflight_timeout",
            stage="deadline",
            reason="probe_timeout",
            error="deploy plan probe timed out before begin",
        ):
            self._resolve_probe(probe_id, PlanRejected("preflight timeout", 504))

    async def _attempt_timeout(self, orchestrator_attempt_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        attempt = await self.attempts.get_attempt(orchestrator_attempt_id)
        if attempt is None or attempt["outcome"] != "active":
            return
        if attempt["execution_mode"] == "evidence_recovery":
            error = "manifest recovery evidence was not received before the attempt deadline"
        elif attempt["result_status"] == "success":
            error = "settled HEAD evidence was not received after a successful deploy result"
        elif attempt["settled_at"] is not None:
            error = "deploy result was not received after settled HEAD evidence"
        else:
            error = "deploy result and settled HEAD evidence were not received before the attempt deadline"
        result = await self.attempts.fail_active_attempt(
            orchestrator_attempt_id,
            kind="attempt_timeout",
            error=error,
        )
        if result.get("deploy_id"):
            await self._after_store_terminal(
                attempt["node_id"], orchestrator_attempt_id, result
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
            requested_orchestrator_attempt_id=probe["requested_orchestrator_attempt_id"],
        )

    async def _send_auto_retry_rejection(
        self,
        msg: RepoReconciliation,
        requested: str,
        generation: str,
    ) -> None:
        await self._send(
            msg.node_id,
            RejectedDeployAttemptAck(
                accepted=False,
                requested_orchestrator_attempt_id=requested,
                deploy_id=msg.deploy_id,
                connection_generation=generation,
                error="retry_requires_manual_approval",
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
        generation = self._generations.get(node_id)
        if generation is None:
            raise PlanRejected("node not connected", 503)
        return generation

    def _cancel_timer(self, key: str) -> None:
        task = self._timers.pop(key, None)
        if task is not None:
            task.cancel()

    @staticmethod
    def _deadline(seconds: float) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
