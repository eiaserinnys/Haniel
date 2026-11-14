"""Node client wire contracts for version 1."""

import pytest
import asyncio
from haniel.config.model import OrchestratorClientConfig
from haniel.integrations.orchestrator_client import OrchestratorClient
from haniel.integrations.deploy_reporting import ApprovalRevalidationError


@pytest.mark.asyncio
async def test_approval_is_acknowledged_and_same_request_declined(tmp_path):
    client = OrchestratorClient(
        OrchestratorClientConfig(url="ws://localhost", token="x", node_id="n"),
        haniel_version="test",
        config_dir=tmp_path,
    )
    sent = []

    async def send(message):
        sent.append(message)

    client._send_json = send
    client._sequencer.start()
    first = {
        "type": "deploy_approval",
        "deploy_id": "n:r:main:sha",
        "orchestrator_attempt_id": "a",
        "connection_generation": "g",
    }
    second = {**first, "orchestrator_attempt_id": "b"}
    try:
        await client._handle_server_message(first)
        await client._handle_server_message(second)
        assert any(message["type"] == "deploy_attempt_received" for message in sent)
        assert any(message.get("reason") == "already_held" for message in sent)
    finally:
        await client._sequencer.close()


@pytest.mark.asyncio
async def test_terminal_ack_removes_only_matching_bundle(tmp_path):
    client = OrchestratorClient(
        OrchestratorClientConfig(url="ws://localhost", token="x", node_id="n"),
        haniel_version="test",
        config_dir=tmp_path,
    )
    client.enqueue_deploy_result(
        "n:r:main:sha", "failed", orchestrator_attempt_id="a", connection_generation="g"
    )
    await client._handle_server_message(
        {"type": "deploy_report_ack", "orchestrator_attempt_id": "a"}
    )
    assert client._registry.outbox.letters()
    await client._handle_server_message(
        {
            "type": "deploy_terminal_received",
            "orchestrator_attempt_id": "a",
            "node_attempt_id": None,
            "deploy_id": "n:r:main:sha",
            "outcome": "closed_now",
        }
    )
    assert not client._registry.outbox.letters()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [ApprovalRevalidationError("changed"), OSError("git unavailable")]
)
async def test_revalidation_failure_occurs_before_start(tmp_path, failure):
    def prepare(_approval):
        raise failure

    client = OrchestratorClient(
        OrchestratorClientConfig(url="ws://localhost", token="x", node_id="n"),
        haniel_version="test",
        config_dir=tmp_path,
        deploy_prepare_handler=prepare,
    )
    sent = []

    async def send(message):
        sent.append(message)

    client._send_json = send
    client._sequencer.start()
    try:
        await client._handle_server_message(
            {
                "type": "deploy_approval",
                "deploy_id": "n:r:main:sha",
                "orchestrator_attempt_id": "a",
                "connection_generation": "g",
            }
        )
        for _ in range(100):
            if any(m.get("kind") == "approval_revalidation_failed" for m in sent):
                break
            await asyncio.sleep(0)
        assert any(m.get("kind") == "approval_revalidation_failed" for m in sent)
        assert not any(m.get("type") == "deploy_progress" for m in sent)
        assert client._registry.runs["a"]["row"]["exec_"] == "declined"
        assert not client._registry.outbox.letters()
    finally:
        await client._sequencer.close()


@pytest.mark.asyncio
async def test_unexpected_auto_permission_is_declined(tmp_path):
    client = OrchestratorClient(
        OrchestratorClientConfig(url="ws://localhost", token="x", node_id="n"),
        haniel_version="test",
        config_dir=tmp_path,
    )
    sent = []

    async def send(message):
        sent.append(message)

    client._send_json = send
    await client._handle_server_message(
        {
            "type": "deploy_attempt_ack",
            "accepted": True,
            "requested_orchestrator_attempt_id": "a",
            "begun_orchestrator_attempt_id": "a",
            "deploy_id": "n:r:main:sha",
            "connection_generation": "g",
            "probe_id": "p",
            "execution_mode": "execute",
            "preflight_fingerprint": "fp",
        }
    )
    assert sent[0]["reason"] == "no_waiter"


@pytest.mark.asyncio
async def test_node_owned_start_report_is_preserved(tmp_path):
    client = OrchestratorClient(
        OrchestratorClientConfig(url="ws://localhost", token="x", node_id="n"),
        haniel_version="test",
        config_dir=tmp_path,
    )
    client.enqueue_node_deploy_report(
        phase="started",
        node_attempt_id="local",
        journal_attempt_id="journal",
        deploy_id="n:r:main:sha",
        repo="r",
        branch="main",
        target_head="sha",
        local_head="old",
        trigger="local",
    )
    assert client._pending_deploy_results[0]["phase"] == "started"
    assert client._registry.held()[0]["state"] == "running"


@pytest.mark.asyncio
async def test_restored_debt_duplicate_replays_and_ack_changes_state(tmp_path):
    client = OrchestratorClient(
        OrchestratorClientConfig(url="ws://localhost", token="x", node_id="n"),
        haniel_version="test",
        config_dir=tmp_path,
    )
    client.enqueue_deploy_result(
        "n:r:main:sha", "failed", orchestrator_attempt_id="a", connection_generation="g"
    )
    assert client._registry.permit("n:r:main:sha", "a", auto=False, waiting=True) == (
        "bundle",
        None,
    )
    await client._handle_server_message(
        {
            "type": "deploy_terminal_received",
            "orchestrator_attempt_id": "a",
            "node_attempt_id": None,
        }
    )
    assert client._registry.runs["a"]["row"]["report"] == "r_acked"
    sent = []

    async def send(message):
        sent.append(message)

    client._send_json = send
    await client._admit_approval(
        {
            "deploy_id": "n:r:main:sha",
            "orchestrator_attempt_id": "a",
            "connection_generation": "g",
        }
    )
    assert sent[0]["type"] == "deploy_result"


@pytest.mark.asyncio
async def test_evidence_recovery_never_announces_execution_on_reconnect_or_duplicate(
    tmp_path,
):
    client = OrchestratorClient(
        OrchestratorClientConfig(url="ws://localhost", token="x", node_id="n"),
        haniel_version="test",
        config_dir=tmp_path,
    )
    sent = []

    async def send(message):
        sent.append(message)

    client._send_json = send
    approval = {
        "type": "deploy_approval",
        "deploy_id": "n:r:main:sha",
        "orchestrator_attempt_id": "a",
        "connection_generation": "g",
        "execution_mode": "evidence_recovery",
    }
    client._registry.permit(approval["deploy_id"], "a", auto=False, waiting=True)
    client._registry.runs["a"]["approval"] = approval
    client._registry.start("a")
    await client._admit_approval(approval)
    for msg, todo in client._registry.connection(True):
        if msg is not None and "started" in todo:
            await client._send_started(msg)
    assert not any(msg["type"] == "deploy_progress" for msg in sent)


@pytest.mark.asyncio
async def test_evidence_recovery_completion_emits_no_progress(tmp_path):
    evidence = {
        "type": "manifest_recovery_evidence",
        "deploy_id": "n:r:main:sha",
        "orchestrator_attempt_id": "a",
    }
    client = OrchestratorClient(
        OrchestratorClientConfig(url="ws://localhost", token="x", node_id="n"),
        haniel_version="test",
        config_dir=tmp_path,
        deploy_approval_handler=lambda *_: evidence,
    )
    sent = []

    async def send(message):
        sent.append(message)

    client._send_json = send
    client._connected = True
    client._ws = object()
    client._registry.permit(evidence["deploy_id"], "a", auto=False, waiting=True)
    await client._handle_deploy_approval(
        {
            "deploy_id": evidence["deploy_id"],
            "orchestrator_attempt_id": "a",
            "connection_generation": "g",
            "execution_mode": "evidence_recovery",
        }
    )
    assert any(msg["type"] == "manifest_recovery_evidence" for msg in sent)
    assert not any(msg["type"] == "deploy_progress" for msg in sent)


@pytest.mark.asyncio
async def test_late_branch_revalidation_failure_precedes_progress(tmp_path):
    def handler(_approval, _progress):
        raise ApprovalRevalidationError("branch changed after preparation")

    client = OrchestratorClient(
        OrchestratorClientConfig(url="ws://localhost", token="x", node_id="n"),
        haniel_version="test",
        config_dir=tmp_path,
        deploy_prepare_handler=lambda _: (None, None),
        deploy_approval_handler=handler,
    )
    sent = []

    async def send(message):
        sent.append(message)

    client._send_json = send
    client._connected = True
    client._ws = object()
    client._sequencer.start()
    try:
        await client._handle_server_message(
            {
                "type": "deploy_approval",
                "deploy_id": "n:r:main:sha",
                "orchestrator_attempt_id": "a",
                "connection_generation": "g",
                "execution_mode": "execute",
            }
        )
        for _ in range(1000):
            if any(m.get("kind") == "approval_revalidation_failed" for m in sent):
                break
            await asyncio.sleep(0.001)
        assert any(m.get("kind") == "approval_revalidation_failed" for m in sent)
        assert not any(m.get("type") == "deploy_progress" for m in sent)
        assert client._registry.runs["a"]["row"]["exec_"] == "declined"
    finally:
        await client._sequencer.close()
