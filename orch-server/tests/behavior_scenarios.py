"""동작 기준 시나리오 (카드 #1259 PR 1).

기준 커밋 155b69f에서 정상 경로와 대표 실패 경로를 돌려 입출력(REST 응답, 바뀐 줄,
방송 글, 저장소 반환값)을 `fixtures/behavior_baseline.json`으로 떴다.
이 파일은 공개 API만 쓰므로 옮긴 뒤의 코드에서도 그대로 돈다.
fixture는 다시 뜨지 않는다. 다시 뜨면 바뀐 동작이 기준이 된다.

    python tests/behavior_scenarios.py --write   # 기준 커밋에서 한 번만
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from haniel_orch.event_store import EventStore
from haniel_orch.protocol import (
    DeployAttemptTerminal,
    DeployPlanProposal,
    ManifestRecoveryEvidence,
    NodeDeployReport,
    RepoReconciliation,
)
from haniel_orch.reconciliation import RepoReconciler

FIXTURE = Path(__file__).parent / "fixtures" / "behavior_baseline.json"
TIME_KEYS = {
    "created_at",
    "updated_at",
    "detected_at",
    "started_at",
    "completed_at",
    "deadline_at",
    "added_at",
    "settled_at",
    "postponed_until",
}


def _later(seconds: int = 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _past() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()


def _norm(value: Any) -> Any:
    """시각 칸은 있고 없고만 남긴다."""
    if isinstance(value, dict):
        return {
            key: (None if item is None else True) if key in TIME_KEYS else _norm(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_norm(item) for item in value]
    return value


EVENT_COLUMNS = (
    "deploy_id, node_id, repo, branch, status, approved_by, reject_reason, error, "
    "duration_ms, is_self_update, target_head, postponed_until, updated_at"
)
ATTEMPT_COLUMNS = (
    "orchestrator_attempt_id, deploy_id, execution_mode, approved_by, outcome, "
    "terminal_kind, terminal_stage, terminal_reason, terminal_error, result_status, "
    "result_error, duration_ms, settled_local_head, settled_remote_head, settled_at, "
    "completed_at"
)


async def _rows(store: EventStore, sql: str) -> list[dict[str, Any]]:
    cursor = await store._db.execute(sql)
    names = [column[0] for column in cursor.description]
    return [_norm(dict(zip(names, row))) for row in await cursor.fetchall()]


async def snapshot(store: EventStore) -> dict[str, Any]:
    """바뀐 줄: 네 표와 딸린 줄, 계획 묻기, 노드 시도."""
    return {
        "events": await _rows(
            store, f"SELECT {EVENT_COLUMNS} FROM deploy_events ORDER BY deploy_id"
        ),
        "attempts": await _rows(
            store,
            f"SELECT {ATTEMPT_COLUMNS} FROM deploy_attempts "
            "ORDER BY orchestrator_attempt_id",
        ),
        "markers": await _rows(
            store,
            "SELECT deploy_id, source_orchestrator_attempt_id, "
            "last_failed_orchestrator_attempt_id FROM deploy_retry_requirements "
            "ORDER BY deploy_id",
        ),
        "lineage": await _rows(
            store,
            "SELECT deploy_id, lineage_orchestrator_attempt_id "
            "FROM deploy_retry_source_attempts ORDER BY deploy_id, "
            "lineage_orchestrator_attempt_id",
        ),
        "probes": await _rows(
            store,
            "SELECT probe_id, deploy_id, status, mode, terminal_kind, terminal_stage, "
            "terminal_reason, terminal_error FROM deploy_plan_probes ORDER BY probe_id",
        ),
        "node_attempts": await _rows(
            store,
            "SELECT node_attempt_id, deploy_id, outcome, terminal_local_head, error, "
            "duration_ms FROM node_deploy_attempts ORDER BY node_attempt_id",
        ),
    }


async def history(store: EventStore) -> list[dict[str, Any]]:
    rows = await store.get_deploy_history(limit=200, include_superseded=True)
    rows = [_norm(row) for row in rows]
    return sorted(rows, key=lambda row: row["deploy_id"])


async def _create(
    store: EventStore,
    deploy_id: str,
    *,
    node: str = "n",
    repo: str = "r",
    branch: str = "main",
    detected: str = "2026-08-01T00:00:00Z",
    target: str | None = None,
    self_update: bool = False,
    manifest: bool = False,
) -> bool:
    return await store.create_deploy_event(
        deploy_id=deploy_id,
        node_id=node,
        repo=repo,
        branch=branch,
        commits=["c"],
        affected_services=["svc"],
        diff_stat="1 file",
        detected_at=detected,
        target_head=target or deploy_id.rsplit(":", 1)[-1],
        is_self_update=self_update,
        deployment_kind="manifest" if manifest else "legacy",
        expected_manifest_identity="release.json" if manifest else None,
        expected_manifest_digest="digest" if manifest else None,
    )


async def _begin(
    store: EventStore, attempt: str, deploy_id: str, generation: str = "g1"
) -> dict[str, Any]:
    return await store.attempts.begin_normal_attempt(
        orchestrator_attempt_id=attempt,
        deploy_id=deploy_id,
        connection_generation=generation,
        current_generation=generation,
        source="manual_single",
        approved_by="director",
        deadline_at=_later(),
    )


def _node_report(
    phase: str, deploy_id: str, *, attempt: str = "na1", repo: str = "r", error=None
) -> NodeDeployReport:
    target = deploy_id.rsplit(":", 1)[-1]
    return NodeDeployReport(
        phase=phase,
        node_attempt_id=attempt,
        journal_attempt_id=f"journal-{attempt}",
        deploy_id=deploy_id,
        node_id="n",
        repo=repo,
        branch="main",
        target_head=target,
        local_head="old" if phase == "started" else target,
        trigger="local",
        error=error,
        duration_ms=None if phase == "started" else 25,
    )


async def _reject(store: EventStore, deploy_id: str, reason: str) -> None:
    """기준 커밋의 일반 쓰기 API와 옮긴 뒤의 거절 API를 같은 뜻으로 부른다."""
    if hasattr(store, "reject_deploy"):
        await store.reject_deploy(deploy_id, reason)
        return
    from haniel_orch.protocol import DeployStatus

    await store.update_deploy_status(
        deploy_id, DeployStatus.REJECTED, reject_reason=reason
    )


async def _reload_foreign_rows(store: EventStore) -> None:
    """옛 서버가 넣고 간 줄은 기동 때 한 번 훑는 스키마 초기화가 맞춘다(기준 커밋에서는 아무 일도 없다)."""
    from haniel_orch.deploy_attempt_schema import initialize_attempt_schema

    await initialize_attempt_schema(store._db)
    await store._db.commit()


async def _step(out: list, name: str, store: EventStore, result: Any) -> None:
    out.append({"step": name, "result": _norm(result), "rows": await snapshot(store)})


async def s01_create_supersedes_older_pending(store: EventStore) -> list:
    out: list = []
    await _step(out, "first", store, await _create(store, "n:r:main:a", detected="2026-08-01T00:00:00Z"))
    await _step(out, "second", store, await _create(store, "n:r:main:b", detected="2026-08-02T00:00:00Z"))
    await _step(out, "duplicate", store, await _create(store, "n:r:main:b", detected="2026-08-02T00:00:00Z"))
    await _step(out, "third", store, await _create(store, "n:r:main:c", detected="2026-08-03T00:00:00Z"))
    await _step(out, "other-branch", store, await _create(store, "n:r:dev:d", branch="dev"))
    out.append({"step": "history", "result": await history(store)})
    return out


async def s02_normal_success(store: EventStore) -> list:
    out: list = []
    deploy = "n:r:main:t1"
    await _create(store, deploy)
    await _step(out, "begin", store, await _begin(store, "a1", deploy))
    attempts = store.attempts
    await _step(
        out,
        "result",
        store,
        await attempts.record_result(
            deploy_id=deploy,
            orchestrator_attempt_id="a1",
            connection_generation="g1",
            status="success",
            error=None,
            duration_ms=321,
        ),
    )
    await _step(
        out,
        "settled",
        store,
        await attempts.record_settled(
            deploy_id=deploy,
            orchestrator_attempt_id="a1",
            connection_generation="g1",
            local_head="t1",
            remote_head="t1-remote-ahead",
        ),
    )
    await _step(
        out,
        "late-result-ignored",
        store,
        await attempts.record_result(
            deploy_id=deploy,
            orchestrator_attempt_id="a1",
            connection_generation="g1",
            status="success",
            error=None,
            duration_ms=1,
        ),
    )
    out.append({"step": "history", "result": await history(store)})
    return out


async def s03_settled_first_then_result(store: EventStore) -> list:
    out: list = []
    deploy = "n:r:main:t2"
    await _create(store, deploy)
    await _begin(store, "a1", deploy)
    attempts = store.attempts
    await _step(
        out,
        "settled",
        store,
        await attempts.record_settled(
            deploy_id=deploy,
            orchestrator_attempt_id="a1",
            connection_generation="g1",
            local_head="t2",
            remote_head="t2",
        ),
    )
    await _step(
        out,
        "wrong-generation-ignored",
        store,
        await attempts.record_result(
            deploy_id=deploy,
            orchestrator_attempt_id="a1",
            connection_generation="other",
            status="success",
            error=None,
            duration_ms=5,
        ),
    )
    await _step(
        out,
        "result",
        store,
        await attempts.record_result(
            deploy_id=deploy,
            orchestrator_attempt_id="a1",
            connection_generation="g1",
            status="success",
            error=None,
            duration_ms=77,
        ),
    )
    return out


async def s04_failed_result_then_retry(store: EventStore) -> list:
    out: list = []
    deploy = "n:r:main:t3"
    await _create(store, deploy)
    await _begin(store, "a1", deploy)
    attempts = store.attempts
    await _step(
        out,
        "failed-result",
        store,
        await attempts.record_result(
            deploy_id=deploy,
            orchestrator_attempt_id="a1",
            connection_generation="g1",
            status="failed",
            error="boom",
            duration_ms=9,
        ),
    )
    await _step(out, "begin-refused-needs-preflight", store, await _expect_error(_begin(store, "a2", deploy)))
    await attempts.create_probe(
        probe_id="p1",
        deploy_id=deploy,
        connection_generation="g2",
        deadline_at=_later(),
        manual_retry=True,
    )
    proposal = DeployPlanProposal(
        mode="execute",
        probe_id="p1",
        connection_generation="g2",
        deploy_id=deploy,
        node_id="n",
        repo="r",
        branch="main",
        target_head="t3",
        current_head="old",
        journal_status=None,
        linked_orchestrator_attempt_id="a1",
        reason="legacy_retry",
        fingerprint="fp1",
    )
    await _step(
        out,
        "begin-retry",
        store,
        await attempts.record_proposal_and_begin(
            proposal,
            orchestrator_attempt_id="a2",
            source="manual_single",
            approved_by="director",
            deadline_at=_later(),
            current_generation="g2",
        ),
    )
    await _step(
        out,
        "second-failure-keeps-source",
        store,
        await attempts.record_node_terminal(
            DeployAttemptTerminal(
                deploy_id=deploy,
                orchestrator_attempt_id="a2",
                connection_generation="g2",
                kind="approval_revalidation_failed",
                reason="approval_revalidation_failed",
                error="journal changed",
            )
        ),
    )
    await _step(
        out,
        "late-failure-ignored",
        store,
        await attempts.record_node_terminal(
            DeployAttemptTerminal(
                deploy_id=deploy,
                orchestrator_attempt_id="a1",
                connection_generation="g1",
                kind="approval_revalidation_failed",
                reason="approval_revalidation_failed",
                error="late",
            )
        ),
    )
    out.append({"step": "history", "result": await history(store)})
    return out


async def _expect_error(coro) -> dict[str, str]:
    try:
        await coro
    except Exception as exc:  # noqa: BLE001 - 기준 동작은 예외 모양까지 견준다
        return {"error": type(exc).__name__, "message": str(exc)}
    return {"error": None}


async def s05_failure_with_newer_canonical(store: EventStore) -> list:
    out: list = []
    old = "n:r:main:old1"
    await _create(store, old, detected="2026-08-01T00:00:00Z")
    await _begin(store, "a1", old)
    await _step(out, "newer-created", store, await _create(store, "n:r:main:new1", detected="2026-08-05T00:00:00Z"))
    await _step(
        out,
        "old-fails",
        store,
        await store.attempts.record_result(
            deploy_id=old,
            orchestrator_attempt_id="a1",
            connection_generation="g1",
            status="failed",
            error="late",
            duration_ms=3,
        ),
    )
    out.append({"step": "history", "result": await history(store)})
    return out


async def s06_settled_mismatch_and_deadline(store: EventStore) -> list:
    out: list = []
    d1 = "n:r:main:m1"
    await _create(store, d1)
    await _begin(store, "a1", d1)
    await _step(
        out,
        "settled-mismatch",
        store,
        await store.attempts.record_settled(
            deploy_id=d1,
            orchestrator_attempt_id="a1",
            connection_generation="g1",
            local_head="wrong",
            remote_head="r",
        ),
    )
    d2 = "n2:r:main:m2"
    await _create(store, d2, node="n2")
    await store.attempts.begin_normal_attempt(
        orchestrator_attempt_id="a2",
        deploy_id=d2,
        connection_generation="g1",
        current_generation="g1",
        source="auto",
        approved_by="director",
        deadline_at=_past(),
    )
    attempt = await store.attempts.get_attempt("a2")
    await _step(
        out,
        "deadline-not-matching",
        store,
        await store.attempts.fail_attempt_if_deadline_due(
            "a2",
            expected_deadline_at="mismatch",
            kind="attempt_timeout",
            stage="deadline",
            reason="deploy_result_timeout",
            error="timeout",
        ),
    )
    await _step(
        out,
        "deadline-due",
        store,
        await store.attempts.fail_attempt_if_deadline_due(
            "a2",
            expected_deadline_at=attempt["deadline_at"],
            kind="attempt_timeout",
            stage="deadline",
            reason="deploy_result_timeout",
            error="timeout",
        ),
    )
    await _step(
        out,
        "renew-after-close-ignored",
        store,
        await store.attempts.renew_active_attempt(
            deploy_id=d2,
            orchestrator_attempt_id="a2",
            connection_generation="g1",
            expected_budget_sec=10,
            deadline_at=_later(),
        ),
    )
    d3 = "n3:r:main:m3"
    await _create(store, d3, node="n3")
    await store.attempts.begin_normal_attempt(
        orchestrator_attempt_id="a3",
        deploy_id=d3,
        connection_generation="g1",
        current_generation="g1",
        source="auto",
        approved_by="director",
        deadline_at=_later(),
    )
    await _step(
        out,
        "renew",
        store,
        await store.attempts.renew_active_attempt(
            deploy_id=d3,
            orchestrator_attempt_id="a3",
            connection_generation="g1",
            expected_budget_sec=10,
            deadline_at="2099-01-01T00:00:00+00:00",
        ),
    )
    return out


async def s07_mode_mismatch_and_recovery(store: EventStore) -> list:
    out: list = []
    deploy = "n:r:main:rec"
    await _create(store, deploy, manifest=True)
    await _begin(store, "source", deploy)
    attempts = store.attempts
    await _step(
        out,
        "evidence-in-execute-mode",
        store,
        await attempts.record_recovery_evidence(
            ManifestRecoveryEvidence(
                deploy_id=deploy,
                orchestrator_attempt_id="source",
                source_orchestrator_attempt_id="source",
                journal_attempt_id="journal-1",
                connection_generation="g1",
                node_id="n",
                repo="r",
                branch="main",
                target_head="rec",
                current_head="rec",
                manifest_identity="release.json",
                manifest_digest="digest",
                journal_status="success",
                journal_completed_at="2026-08-01T00:01:00Z",
            )
        ),
    )
    await attempts.create_probe(
        probe_id="rp",
        deploy_id=deploy,
        connection_generation="g2",
        deadline_at=_later(),
        manual_retry=True,
    )
    proposal = DeployPlanProposal(
        mode="evidence_recovery",
        probe_id="rp",
        connection_generation="g2",
        deploy_id=deploy,
        node_id="n",
        repo="r",
        branch="main",
        target_head="rec",
        current_head="rec",
        journal_status="success",
        journal_attempt_id="journal-1",
        linked_orchestrator_attempt_id="source",
        journal_target_head="rec",
        manifest_identity="release.json",
        manifest_digest="digest",
        journal_completed_at="2026-08-01T00:01:00Z",
        reason="durable_local_success",
        fingerprint="rfp",
    )
    await _step(
        out,
        "begin-recovery",
        store,
        await attempts.record_proposal_and_begin(
            proposal,
            orchestrator_attempt_id="recovery",
            source="manual_single",
            approved_by="director",
            deadline_at=_later(),
            current_generation="g2",
        ),
    )
    await _step(
        out,
        "raw-result-in-recovery-mode",
        store,
        await attempts.record_result(
            deploy_id=deploy,
            orchestrator_attempt_id="recovery",
            connection_generation="g2",
            status="success",
            error=None,
            duration_ms=1,
        ),
    )
    out.append({"step": "history", "result": await history(store)})
    return out


async def s08_recovery_success(store: EventStore) -> list:
    out: list = []
    deploy = "n:r:main:rec2"
    await _create(store, deploy, manifest=True)
    await _begin(store, "source", deploy)
    attempts = store.attempts
    await attempts.fail_active_attempt(
        "source", kind="attempt_timeout", stage="deadline", reason="lost", error="lost"
    )
    await attempts.create_probe(
        probe_id="rp",
        deploy_id=deploy,
        connection_generation="g2",
        deadline_at=_later(),
        manual_retry=True,
    )
    proposal = DeployPlanProposal(
        mode="evidence_recovery",
        probe_id="rp",
        connection_generation="g2",
        deploy_id=deploy,
        node_id="n",
        repo="r",
        branch="main",
        target_head="rec2",
        current_head="rec2",
        journal_status="success",
        journal_attempt_id="journal-1",
        linked_orchestrator_attempt_id="source",
        journal_target_head="rec2",
        manifest_identity="release.json",
        manifest_digest="digest",
        journal_completed_at="2026-08-01T00:01:00Z",
        reason="durable_local_success",
        fingerprint="rfp",
    )
    await attempts.record_proposal_and_begin(
        proposal,
        orchestrator_attempt_id="recovery",
        source="manual_single",
        approved_by="director",
        deadline_at=_later(),
        current_generation="g2",
    )
    bad = ManifestRecoveryEvidence(
        deploy_id=deploy,
        orchestrator_attempt_id="recovery",
        source_orchestrator_attempt_id="source",
        journal_attempt_id="journal-WRONG",
        connection_generation="g2",
        node_id="n",
        repo="r",
        branch="main",
        target_head="rec2",
        current_head="rec2",
        manifest_identity="release.json",
        manifest_digest="digest",
        journal_status="success",
        journal_completed_at="2026-08-01T00:01:00Z",
    )
    await _step(out, "invalid-evidence-fails", store, await attempts.record_recovery_evidence(bad))
    out.append({"step": "history", "result": await history(store)})
    return out


async def s09_recovery_success_valid(store: EventStore) -> list:
    out: list = []
    deploy = "n:r:main:rec3"
    await _create(store, deploy, manifest=True)
    await _begin(store, "source", deploy)
    attempts = store.attempts
    await attempts.fail_active_attempt(
        "source", kind="attempt_timeout", stage="deadline", reason="lost", error="lost"
    )
    await attempts.create_probe(
        probe_id="rp",
        deploy_id=deploy,
        connection_generation="g2",
        deadline_at=_later(),
        manual_retry=True,
    )
    proposal = DeployPlanProposal(
        mode="evidence_recovery",
        probe_id="rp",
        connection_generation="g2",
        deploy_id=deploy,
        node_id="n",
        repo="r",
        branch="main",
        target_head="rec3",
        current_head="rec3",
        journal_status="success",
        journal_attempt_id="journal-1",
        linked_orchestrator_attempt_id="source",
        journal_target_head="rec3",
        manifest_identity="release.json",
        manifest_digest="digest",
        journal_completed_at="2026-08-01T00:01:00Z",
        reason="durable_local_success",
        fingerprint="rfp",
    )
    await attempts.record_proposal_and_begin(
        proposal,
        orchestrator_attempt_id="recovery",
        source="manual_single",
        approved_by="director",
        deadline_at=_later(),
        current_generation="g2",
    )
    good = ManifestRecoveryEvidence(
        deploy_id=deploy,
        orchestrator_attempt_id="recovery",
        source_orchestrator_attempt_id="source",
        journal_attempt_id="journal-1",
        connection_generation="g2",
        node_id="n",
        repo="r",
        branch="main",
        target_head="rec3",
        current_head="rec3",
        manifest_identity="release.json",
        manifest_digest="digest",
        journal_status="success",
        journal_completed_at="2026-08-01T00:01:00Z",
    )
    await _step(out, "recovery-success", store, await attempts.record_recovery_evidence(good))
    out.append({"step": "history", "result": await history(store)})
    return out


async def s10_node_reports(store: EventStore) -> list:
    out: list = []
    pend = "n:r:main:np"
    await _create(store, pend, branch="main")
    await _step(out, "started", store, await store.record_node_deploy_report(_node_report("started", pend)))
    await _step(
        out,
        "failed-on-pending",
        store,
        await store.record_node_deploy_report(_node_report("failed", pend, error="verify failed")),
    )
    await _step(
        out,
        "succeeded-on-pending",
        store,
        await store.record_node_deploy_report(_node_report("succeeded", pend, attempt="na2")),
    )
    await _step(
        out,
        "report-after-close-ignored",
        store,
        await store.record_node_deploy_report(_node_report("succeeded", pend, attempt="na3")),
    )

    dep = "n2:r:main:nd"
    await _create(store, dep, node="n2")
    await store.attempts.begin_normal_attempt(
        orchestrator_attempt_id="oa1",
        deploy_id=dep,
        connection_generation="g1",
        current_generation="g1",
        source="manual_single",
        approved_by="director",
        deadline_at=_later(),
    )
    rep = _node_report("failed", dep, attempt="nb1", error="own failure")
    rep = rep.model_copy(update={"node_id": "n2"})
    await _step(out, "failed-with-active-attempt", store, await store.record_node_deploy_report(rep))
    rep2 = _node_report("succeeded", dep, attempt="nb2").model_copy(update={"node_id": "n2"})
    await _step(out, "succeeded-supersedes-attempt", store, await store.record_node_deploy_report(rep2))

    dep3 = "n3:r:main:ne"
    await _create(store, dep3, node="n3")
    await store.attempts.begin_normal_attempt(
        orchestrator_attempt_id="oa3",
        deploy_id=dep3,
        connection_generation="g1",
        current_generation="g1",
        source="manual_single",
        approved_by="director",
        deadline_at=_later(),
    )
    await store.attempts.record_node_terminal(
        DeployAttemptTerminal(
            deploy_id=dep3,
            orchestrator_attempt_id="oa3",
            connection_generation="g1",
            kind="approval_revalidation_failed",
            reason="approval_revalidation_failed",
            error="x",
        )
    )
    rep3 = _node_report("failed", dep3, attempt="nc1", error="node own failure").model_copy(
        update={"node_id": "n3"}
    )
    await _step(out, "failed-on-retry-pending-keeps-marker", store, await store.record_node_deploy_report(rep3))
    out.append({"step": "history", "result": await history(store)})
    return out


class _Broadcasts:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)


def _observed(deploy_id: str, local: str, remote: str, repo: str = "r") -> RepoReconciliation:
    return RepoReconciliation(
        phase="observed",
        deploy_id=deploy_id,
        node_id="n",
        repo=repo,
        branch="main",
        local_head=local,
        remote_head=remote,
    )


async def s11_observation(store: EventStore) -> list:
    out: list = []
    broadcasts = _Broadcasts()
    reconciler = RepoReconciler(store, broadcasts)

    async def observe(name: str, msg: RepoReconciliation) -> None:
        before = len(broadcasts.messages)
        await reconciler.handle(msg)
        await _step(out, name, store, {"broadcasts": broadcasts.messages[before:]})

    plain = "n:r:main:o1"
    await _create(store, plain)
    await observe("heads-differ-keeps-pending", _observed(plain, "o1", "other"))
    await observe("closes-plain-pending", _observed(plain, "o1", "o1"))

    marked = "n:r2:main:o2"
    await _create(store, marked, repo="r2")
    await _begin(store, "oa", marked)
    await store.attempts.fail_active_attempt(
        "oa", kind="failed", stage="execution", reason="x", error="boom"
    )
    await observe("retry-marker-blocks-regular-close", _observed(marked, "o2", "o2", repo="r2"))

    active = "n:r3:main:o3"
    await _create(store, active, repo="r3")
    await _begin(store, "oa3", active)
    await observe("active-attempt-blocks-close", _observed(active, "o3", "o3", repo="r3"))

    nodefail = "n:r4:main:o4"
    await _create(store, nodefail, repo="r4")
    await store.record_node_deploy_report(
        _node_report("started", nodefail, attempt="nf", repo="r4")
    )
    await store.record_node_deploy_report(
        _node_report("failed", nodefail, attempt="nf", repo="r4", error="e")
    )
    await observe("failed-node-attempt-blocks-close", _observed(nodefail, "o4", "o4", repo="r4"))

    # 자기 갱신: 시간 초과로 머리가 옛것인 줄을 더 새 관측이 닫는다
    selfupd = "n:haniel:main:old-head"
    await _create(store, selfupd, repo="haniel", target="newer-head", self_update=True)
    await observe(
        "self-update-closed-by-newer-observation",
        _observed("n:haniel:main:newer-obs", "newer-head", "newer-head", repo="haniel"),
    )

    regular_marked = "n:r5:main:o5"
    await _create(store, regular_marked, repo="r5", target="t5")
    await _begin(store, "oa5", regular_marked)
    await store.attempts.fail_active_attempt(
        "oa5", kind="failed", stage="execution", reason="x", error="boom"
    )
    await observe(
        "regular-retry-not-closed-by-other-observation",
        _observed("n:r5:main:other", "t5", "t5", repo="r5"),
    )

    selfupd_marked = "n:haniel2:main:s2"
    await _create(store, selfupd_marked, repo="haniel2", target="s2", self_update=True)
    await _begin(store, "oa6", selfupd_marked)
    await store.attempts.fail_active_attempt(
        "oa6", kind="failed", stage="execution", reason="x", error="boom"
    )
    await observe(
        "self-update-with-marker-closed",
        _observed("n:haniel2:main:later", "s2", "s2", repo="haniel2"),
    )

    selfupd_active = "n:haniel3:main:s3"
    await _create(store, selfupd_active, repo="haniel3", target="s3", self_update=True)
    await _begin(store, "oa7", selfupd_active)
    await observe(
        "self-update-active-attempt-blocks",
        _observed("n:haniel3:main:later", "s3", "s3", repo="haniel3"),
    )
    return out


async def s12_postponed_revival(store: EventStore) -> list:
    out: list = []
    sid = "n:haniel:main:p1"
    await _create(store, sid, repo="haniel", self_update=True)
    await _reject(store, sid, "postponed")
    await _step(out, "rejected", store, None)
    await _step(out, "revive-without-postponement", store, await store.reopen_postponed_self_update(sid))
    await store.set_deploy_postponed_until(sid, _later(3600))
    await _step(out, "revive-before-deadline", store, await store.reopen_postponed_self_update(sid))
    await store.set_deploy_postponed_until(sid, _past())
    await _create(store, "n:haniel:main:p2", repo="haniel", detected="2026-09-01T00:00:00Z", self_update=True)
    await _step(out, "revive-blocked-by-other-pending", store, await store.reopen_postponed_self_update(sid))
    return out


async def s13_postponed_revival_clear(store: EventStore) -> list:
    out: list = []
    sid = "n:haniel:main:q1"
    await _create(store, sid, repo="haniel", self_update=True)
    await _reject(store, sid, "postponed")
    await store.set_deploy_postponed_until(sid, _past())
    await _step(out, "revive-due", store, await store.reopen_postponed_self_update(sid))
    await _step(out, "revive-again-no-op", store, await store.reopen_postponed_self_update(sid))
    return out


async def s14_branch_supersede_and_renamed(store: EventStore) -> list:
    out: list = []
    await _create(store, "n:r:main:b1", detected="2026-08-01T00:00:00Z")
    await store._db.execute(
        "INSERT INTO deploy_events (deploy_id,node_id,repo,branch,status,commits_json,"
        "affected_services_json,detected_at,created_at,updated_at,target_head) "
        "VALUES ('n:r:main:b0','n','r','main','pending','[]','[]','2026-07-01T00:00:00Z',"
        "'2026-07-01T00:00:00Z','2026-07-01T00:00:00Z','b0')"
    )
    await store._db.commit()
    await _reload_foreign_rows(store)
    await _step(
        out,
        "supersede-pending-for-branch",
        store,
        [row["deploy_id"] for row in await store.supersede_pending_for_branch("n", "r", "main")],
    )
    await _create(store, "old-node:r:main:x1", node="old-node", repo="r9")
    await _create(store, "other-node:r:main:x2", node="other-node", repo="r9")
    await _step(
        out,
        "node-renamed",
        store,
        [
            row["deploy_id"]
            for row in await store.reject_pending_deploys_for_nodes(["old-node"], "node renamed to new-node")
        ],
    )
    return out


async def s15_startup_supersede(store: EventStore) -> list:
    out: list = []
    await _create(store, "n:r:main:z1", detected="2026-08-01T00:00:00Z")
    for deploy_id, detected in (("n:r:main:z0", "2026-07-01T00:00:00Z"), ("n:r:main:z00", "2026-06-01T00:00:00Z")):
        await store._db.execute(
            "INSERT INTO deploy_events (deploy_id,node_id,repo,branch,status,commits_json,"
            "affected_services_json,detected_at,created_at,updated_at,target_head) "
            "VALUES (?,'n','r','main','pending','[]','[]',?,?,?,'z')",
            (deploy_id, detected, detected, detected),
        )
    await store._db.commit()
    await _reload_foreign_rows(store)
    await _step(out, "startup-supersede", store, await store.supersede_stale_pending_deploys())
    return out


SCENARIOS = {
    "s01_create_supersedes_older_pending": s01_create_supersedes_older_pending,
    "s02_normal_success": s02_normal_success,
    "s03_settled_first_then_result": s03_settled_first_then_result,
    "s04_failed_result_then_retry": s04_failed_result_then_retry,
    "s05_failure_with_newer_canonical": s05_failure_with_newer_canonical,
    "s06_settled_mismatch_and_deadline": s06_settled_mismatch_and_deadline,
    "s07_mode_mismatch_and_recovery": s07_mode_mismatch_and_recovery,
    "s08_recovery_invalid": s08_recovery_success,
    "s09_recovery_success": s09_recovery_success_valid,
    "s10_node_reports": s10_node_reports,
    "s11_observation": s11_observation,
    "s12_postponed_revival": s12_postponed_revival,
    "s13_postponed_revival_clear": s13_postponed_revival_clear,
    "s14_branch_supersede_and_renamed": s14_branch_supersede_and_renamed,
    "s15_startup_supersede": s15_startup_supersede,
}


async def run_scenario(name: str) -> list:
    store = EventStore(":memory:")
    await store.initialize()
    try:
        return await SCENARIOS[name](store)
    finally:
        await store.close()


async def run_rest_scenarios() -> dict[str, Any]:
    """REST 응답: 거절, 대기 목록, 이력."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from haniel_orch.api import create_api_routes
    from haniel_orch.hub import WebSocketHub
    from haniel_orch.node_registry import NodeRegistry

    store = EventStore(":memory:")
    await store.initialize()
    try:
        registry = NodeRegistry(store)
        hub = WebSocketHub(registry, store, token="test-token")
        sent: list[dict] = []

        async def capture(message: dict) -> None:
            sent.append(message)

        hub.broadcast_to_dashboards = capture  # type: ignore[method-assign]
        routes = create_api_routes(hub, store)
        client = TestClient(Starlette(routes=routes))
        out: dict[str, Any] = {}

        async def seed(deploy_id: str, **kwargs) -> None:
            await _create(store, deploy_id, **kwargs)

        await seed("n:r:main:r1", detected="2026-08-01T00:00:00Z")
        await seed("n:haniel:main:r2", repo="haniel", self_update=True)
        out["pending-before"] = client.get("/api/orch/pending").json()
        out["pending-before"] = _norm(out["pending-before"])

        def post(body: dict) -> dict:
            response = client.post("/api/orch/reject", json=body)
            return {"status_code": response.status_code, "body": _norm(response.json())}

        out["reject-missing-id"] = post({})
        out["reject-unknown"] = post({"deploy_id": "nope"})
        out["reject-regular"] = post({"deploy_id": "n:r:main:r1", "reason": "no thanks"})
        out["reject-again-409"] = post({"deploy_id": "n:r:main:r1"})
        out["reject-self-update"] = post({"deploy_id": "n:haniel:main:r2"})
        out["broadcasts"] = _norm(sent)
        out["pending-after"] = _norm(client.get("/api/orch/pending").json())
        out["history"] = _norm(client.get("/api/orch/history").json())
        out["rows"] = await snapshot(store)
        return out
    finally:
        await store.close()


async def run_all() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in SCENARIOS:
        result[name] = await run_scenario(name)
    result["rest_reject"] = await run_rest_scenarios()
    return json.loads(json.dumps(result, default=str))


if __name__ == "__main__":
    if "--write" not in sys.argv:
        raise SystemExit("기준 fixture는 기준 커밋에서 --write로 한 번만 뜬다")
    data = asyncio.run(run_all())
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(
        json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {FIXTURE} ({FIXTURE.stat().st_size} bytes)")
