"""design.md 4절 394-396: the receive loop is not blocked by a running deployment,
and a deployment's result survives the socket that approved it."""

from __future__ import annotations

import asyncio

from .conftest import NODE_ID, HeldDeployment, approve_held_deployment


async def test_service_command_is_answered_while_a_deployment_is_held(
    dashboard, http, nodes
) -> None:
    held = HeldDeployment()
    commands: list[tuple[str, str]] = []

    def service_command(service_name: str, action: str, _payload) -> dict:
        commands.append((service_name, action))
        return {"ok": True, "service": service_name, "action": action}

    client = nodes.start(
        approval_handler=held.handle, service_command_handler=service_command
    )
    deploy_id = await approve_held_deployment(client, dashboard, http, held)
    try:
        response = await http.post(
            "/api/orch/service-command",
            json={"node_id": NODE_ID, "service_name": "web", "action": "start"},
        )
        assert response.status_code == 200, response.text
        command_id = response.json()["command_id"]

        result = await dashboard.wait_for(
            lambda e: (
                e["type"] == "service_command_result" and e["command_id"] == command_id
            )
        )
        assert not held.release.is_set(), "the deployment must still be held"
        assert result["success"] is True, result
        assert result["error"] is None
        assert commands == [("web", "start")]
    finally:
        held.release.set()

    closed = await dashboard.wait_for(
        lambda e: (
            e["type"] == "status_change"
            and e["deploy_id"] == deploy_id
            and e["status"] in {"success", "failed", "pending"}
        ),
        timeout=15,
    )
    assert closed["status"] == "success", closed


async def test_result_and_settlement_arrive_after_reconnect(
    dashboard, http, nodes
) -> None:
    held = HeldDeployment()
    client = nodes.start(approval_handler=held.handle, reconnect_base=1.0)
    deploy_id = await approve_held_deployment(client, dashboard, http, held)
    attempt_id = held.approvals[0]["orchestrator_attempt_id"]

    # Drop the socket from the node side, the way a network loss looks to it.
    socket, loop = client._ws, client._loop
    assert socket is not None and loop is not None
    await asyncio.wrap_future(
        asyncio.run_coroutine_threadsafe(
            socket.close(code=1012, reason="contract: drop the node socket"), loop
        )
    )
    await dashboard.wait_for(
        lambda e: e["type"] == "node_disconnected" and e["node_id"] == NODE_ID
    )
    held.release.set()  # the deployment finishes while the node is offline

    await dashboard.wait_for(
        lambda e: e["type"] == "node_connected" and e["node_id"] == NODE_ID,
        timeout=15,
    )
    closed = await dashboard.wait_for(
        lambda e: (
            e["type"] == "status_change"
            and e["deploy_id"] == deploy_id
            and e["status"] in {"success", "failed", "pending"}
        ),
        timeout=15,
    )
    assert closed["status"] == "success", closed

    pending = (await http.get("/api/orch/pending")).json()["deploys"]
    assert deploy_id not in {row["deploy_id"] for row in pending}
    history = (await http.get("/api/orch/history")).json()["deploys"]
    row = next(row for row in history if row["deploy_id"] == deploy_id)
    assert row["status"] == "success", row
    assert len(held.approvals) == 1, "the request must not have been executed twice"
    assert held.approvals[0]["orchestrator_attempt_id"] == attempt_id


async def test_outbox_is_acknowledged_after_hello_reconciliation(
    dashboard, http, nodes
):
    import json
    import threading

    held = HeldDeployment()
    client = nodes.start(approval_handler=held.handle)
    deploy_id = await approve_held_deployment(client, dashboard, http, held)
    attempt = held.approvals[0]["orchestrator_attempt_id"]
    reconnect = threading.Event()
    original_connect = client._connect_and_listen

    async def gated_connect():
        await asyncio.to_thread(reconnect.wait, 15)
        await original_connect()

    client._connect_and_listen = gated_connect
    try:
        await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(client._ws.close(), client._loop)
        )
        await dashboard.wait_for(
            lambda event: (
                event["type"] == "node_disconnected" and event["node_id"] == NODE_ID
            )
        )
        held.release.set()

        async def wait_owed():
            while True:
                with client._registry.lock:
                    if "o:" + attempt in client._registry.outbox.entries:
                        return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_owed(), 10)
        saved = json.loads(client._registry.outbox.path.read_text(encoding="utf-8"))
        assert len(saved["entries"]["o:" + attempt]["letters"]) == 2
        reconnect.set()
        await dashboard.wait_for(
            lambda event: (
                event["type"] == "node_connected" and event["node_id"] == NODE_ID
            ),
            timeout=15,
        )
        closed = await dashboard.wait_for(
            lambda event: (
                event["type"] == "status_change"
                and event["deploy_id"] == deploy_id
                and event["status"] == "success"
            ),
            timeout=15,
        )
        assert closed["status"] == "success"

        async def wait_ack():
            while True:
                with client._registry.lock:
                    if "o:" + attempt not in client._registry.outbox.entries:
                        assert (
                            "o:" + attempt
                            not in json.loads(
                                client._registry.outbox.path.read_text(encoding="utf-8")
                            )["entries"]
                        )
                        return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_ack(), 10)
    finally:
        held.release.set()
        reconnect.set()


async def test_second_attempt_is_declined_and_returns_to_again(
    orchestrator, dashboard, http, nodes
):
    held = HeldDeployment()
    client = nodes.start(approval_handler=held.handle)
    send = client._send_json

    async def lose_first_receipt_and_progress(message):
        if message.get("type") in {"deploy_attempt_received", "deploy_progress"}:
            return
        await send(message)

    client._send_json = lose_first_receipt_and_progress
    deploy_id = await approve_held_deployment(client, dashboard, http, held)
    attempt = held.approvals[0]["orchestrator_attempt_id"]
    try:
        attempts = orchestrator.hub.deploy_coordinator.attempts
        await attempts.fail_attempt_not_sent(attempt)
        response = await http.post(
            "/api/orch/approve",
            json={"deploy_id": deploy_id, "approved_by": "contract"},
        )
        assert response.status_code == 200, response.text

        async def wait_again():
            while True:
                retry = await attempts.get_retry_requirement(deploy_id)
                if (
                    retry
                    and not retry["inspection_required"]
                    and len(retry["lineage"]) >= 2
                ):
                    active = await attempts.get_active_attempts()
                    if not any(row["deploy_id"] == deploy_id for row in active):
                        return retry
                await asyncio.sleep(0.01)

        retry = await asyncio.wait_for(wait_again(), 10)
        second = next(item for item in retry["lineage"] if item != attempt)
        second_row = await attempts.get_attempt(second)
        assert second_row["closure"] == "not_executed"
        assert second_row["terminal_reason"] == "already_held"
        assert len(held.approvals) == 1
    finally:
        held.release.set()


async def test_journal_recovery_uses_original_generation_with_real_hub(
    dashboard, http, nodes, tmp_path
):
    from haniel.config import HanielConfig
    from haniel.core.deployment_state import DeploymentStateStore
    from haniel.core.node_deploy_reporting import enqueue_startup_retro_reports
    from .conftest import NODE_TOKEN, REPO, BRANCH, TARGET

    held = HeldDeployment()
    client = nodes.start(approval_handler=held.handle)
    deploy_id = await approve_held_deployment(client, dashboard, http, held)
    attempt = held.approvals[0]["orchestrator_attempt_id"]
    config = HanielConfig(
        repos={
            REPO: {
                "url": "git@example/repo",
                "path": "repo",
                "branch": BRANCH,
                "release_manifest": "release.json",
            }
        },
        services={},
        orchestrator_client={
            "url": "ws://orch",
            "token": NODE_TOKEN,
            "node_id": NODE_ID,
        },
    )
    store = DeploymentStateStore(tmp_path / "journal")
    store.begin(
        REPO,
        "old",
        TARGET,
        "release",
        orchestrator_attempt_id=attempt,
        node_id=NODE_ID,
        branch=BRANCH,
    )
    store.transition(REPO, "success")
    try:
        enqueue_startup_retro_reports(
            config=config,
            config_dir=tmp_path,
            orchestrator_client=client,
            journal_store=store,
            get_local_head=lambda _: TARGET,
        )
        event = await dashboard.wait_for(
            lambda item: (
                item["type"] == "status_change"
                and item["deploy_id"] == deploy_id
                and item["status"] == "success"
            ),
            timeout=10,
        )
        assert event["status"] == "success"

        async def wait_for_ack():
            while True:
                with client._registry.lock:
                    if "o:" + attempt not in client._registry.outbox.entries:
                        return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_ack(), 10)
    finally:
        held.release.set()
