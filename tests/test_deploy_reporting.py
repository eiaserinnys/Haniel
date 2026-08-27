"""Trigger-independent deploy result and reconciliation contracts."""

from __future__ import annotations

import threading
import time

import pytest

from haniel.core.repo_reconciliation import RepoReconciliationSnapshot
from haniel.integrations.deploy_reporting import (
    DeployReporter,
    classify_deploy_result,
)


def snapshot(*, in_sync: bool) -> RepoReconciliationSnapshot:
    return RepoReconciliationSnapshot(
        node_id="node-a",
        repo="repo-a",
        branch="main",
        local_head="remote-head" if in_sync else "local-head",
        remote_head="remote-head",
        deploy_id="node-a:repo-a:main:remote-head",
    )


@pytest.mark.parametrize("trigger", ["auto_apply", "manual_single", "manual_batch"])
@pytest.mark.parametrize(
    ("operation_error", "in_sync", "expected_status", "pending_visible"),
    [
        (None, True, "success", False),
        (None, False, "failed", True),
        ("hook failed", True, "failed", False),
        ("hook failed", False, "failed", True),
    ],
)
def test_trigger_head_result_matrix_has_one_head_contract(
    trigger: str,
    operation_error: str | None,
    in_sync: bool,
    expected_status: str,
    pending_visible: bool,
) -> None:
    status, error = classify_deploy_result(snapshot(in_sync=in_sync), operation_error)
    assert status == expected_status, trigger
    assert (not in_sync) is pending_visible, trigger
    if not in_sync:
        assert "node=node-a" in error
        assert "repo=repo-a" in error
        assert "branch=main" in error
        assert "local_head=local-head" in error
        assert "remote_head=remote-head" in error


async def test_manual_success_return_with_mismatch_sends_failed_then_settled() -> None:
    sent: list[dict] = []

    async def send_json(message: dict) -> None:
        sent.append(message)

    def handler(_approval, progress):
        progress("build")
        time.sleep(0.035)
        return None

    reporter = DeployReporter(
        node_id="node-a",
        approval_handler=handler,
        snapshot_handler=lambda repo, branch, deploy_id: snapshot(in_sync=False),
        expected_budget_handler=lambda deploy_id: 1938,
        send_json=send_json,
        progress_interval_sec=0.01,
    )

    await reporter.handle_approval(
        {
            "deploy_id": "node-a:repo-a:main:remote-head",
            "orchestrator_attempt_id": "orch-1",
            "connection_generation": "generation-1",
            "execution_mode": "execute",
            "approved_by": "dashboard",
        }
    )

    types = [message["type"] for message in sent]
    assert types[-2:] == [
        "deploy_result",
        "repo_reconciliation",
    ]
    progress = [message for message in sent if message["type"] == "deploy_progress"]
    assert progress[0]["stage"] == "preparing"
    assert progress[0]["expected_budget_sec"] == 1938
    assert all("expected_budget_sec" not in message for message in progress[1:])
    assert any(message["stage"] == "build" for message in progress)
    assert sum(message["stage"] == "build" for message in progress) >= 2
    # Raw operation success and settled HEAD are independent evidence. The
    # server combines them and turns this mismatch into retryable failure.
    result, reconciliation = sent[-2:]
    assert result["status"] == "success"
    assert result["orchestrator_attempt_id"] == "orch-1"
    assert reconciliation["phase"] == "settled"
    assert reconciliation["orchestrator_attempt_id"] == "orch-1"
    assert reconciliation["deploy_id"] == "node-a:repo-a:main:remote-head"


async def test_dependent_readiness_failures_are_reported_with_success() -> None:
    sent: list[dict] = []

    async def send_json(message: dict) -> None:
        sent.append(message)

    reporter = DeployReporter(
        node_id="node-a",
        approval_handler=lambda _approval, _progress: {
            "dependent_readiness_failures": ["bot", "keke"]
        },
        snapshot_handler=lambda repo, branch, deploy_id: snapshot(in_sync=True),
        send_json=send_json,
    )

    await reporter.handle_approval(
        {
            "deploy_id": "node-a:repo-a:main:remote-head",
            "orchestrator_attempt_id": "orch-1",
            "connection_generation": "generation-1",
        }
    )

    result = next(message for message in sent if message["type"] == "deploy_result")
    assert result["status"] == "success"
    assert result["dependent_readiness_failures"] == ["bot", "keke"]


async def test_progress_emitter_stops_when_handler_finishes() -> None:
    sent: list[dict] = []

    async def send_json(message: dict) -> None:
        sent.append(message)

    finished = threading.Event()

    def handler(_approval, progress):
        progress("verifying")
        finished.set()
        return "deferred"

    reporter = DeployReporter(
        node_id="node-a",
        approval_handler=handler,
        snapshot_handler=None,
        expected_budget_handler=lambda deploy_id: 6300,
        send_json=send_json,
        progress_interval_sec=0.01,
    )
    await reporter.handle_approval(
        {
            "deploy_id": "node-a:repo-a:main:remote-head",
            "orchestrator_attempt_id": "orch-1",
            "connection_generation": "generation-1",
        }
    )
    assert finished.is_set()
    progress = [message for message in sent if message["type"] == "deploy_progress"]
    assert progress[0]["expected_budget_sec"] == 6300
    assert all("expected_budget_sec" not in message for message in progress[1:])
    count = len(sent)
    await __import__("asyncio").sleep(0.03)
    assert len(sent) == count


async def test_attempt_snapshot_id_never_enters_node_wire() -> None:
    async def send_json(_message: dict) -> None:
        raise AssertionError("invalid ID must be rejected before send")

    reporter = DeployReporter(
        node_id="node-a",
        approval_handler=None,
        snapshot_handler=None,
        send_json=send_json,
    )
    invalid = snapshot(in_sync=False)
    object.__setattr__(invalid, "deploy_id", "attempt:deadbeef")
    with pytest.raises(ValueError, match="server-only"):
        await reporter.send_reconciliation(invalid, "settled")
