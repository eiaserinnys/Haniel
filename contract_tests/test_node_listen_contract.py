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
