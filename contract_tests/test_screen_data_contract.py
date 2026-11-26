"""카드 #1896: 승인 화면이 읽는 값이 실제 노드 클라이언트에서 허브를 거쳐 REST와 방송으로 나온다."""

from __future__ import annotations

import asyncio

from .conftest import BRANCH, NODE_ID, REPO, TARGET, HeldDeployment

SELF_REPO = "haniel"
SELF_TARGET = "5e1f" + "0" * 36
FETCH_INFO = {
    "repos": [{"repo": REPO, "last_fetch": "2026-11-26T09:00:00+00:00"}],
    "poll_interval": 60,
}


async def connect(dashboard, client) -> None:
    await dashboard.wait_for(
        lambda e: e["type"] == "node_connected" and e["node_id"] == NODE_ID
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not client._connected:
        assert loop.time() < deadline, "node client never marked itself connected"
        await asyncio.sleep(0.01)


async def announce(dashboard, client, repo: str, target: str, **extra) -> str:
    client.notify_change(
        repo,
        BRANCH,
        commits=[f"{target} contract change"],
        affected_services=["web"],
        target_head=target,
        wait=True,
        **extra,
    )
    pending = await dashboard.wait_for(
        lambda e: e["type"] == "new_pending" and e["repo"] == repo
    )
    return pending["deploy_id"]


async def pending_row(http, deploy_id: str) -> dict:
    rows = (await http.get("/api/orch/pending")).json()["deploys"]
    return next(row for row in rows if row["deploy_id"] == deploy_id)


async def node_row(http) -> dict:
    nodes = (await http.get("/api/orch/nodes")).json()["nodes"]
    return next(row for row in nodes if row["node_id"] == NODE_ID)


async def test_policy_fetch_info_stage_and_serving_commit(dashboard, http, nodes):
    held = HeldDeployment()
    client = nodes.start(
        approval_handler=held.handle, get_repo_fetch_info=lambda: FETCH_INFO
    )
    await connect(dashboard, client)

    deploy_id = await announce(dashboard, client, REPO, TARGET, apply_policy="approval")
    assert (await pending_row(http, deploy_id))["apply_policy"] == "approval"

    node = await node_row(http)
    assert node["poll_interval"] == 60
    assert node["repo_fetches"] == FETCH_INFO["repos"]
    assert node["serving"] == []

    response = await http.post(
        "/api/orch/approve", json={"deploy_id": deploy_id, "approved_by": "contract"}
    )
    assert response.status_code == 200, response.text
    await held.wait_entered()
    progress = await dashboard.wait_for(
        lambda e: e["type"] == "deploy_progress" and e["deploy_id"] == deploy_id
    )
    assert progress["stage"] == "preparing"
    assert progress["node_id"] == NODE_ID
    assert (await pending_row(http, deploy_id))["progress_stage"] == "preparing"

    held.release.set()
    await dashboard.wait_for(
        lambda e: (
            e["type"] == "status_change"
            and e["deploy_id"] == deploy_id
            and e["status"] == "success"
        ),
        timeout=15,
    )
    (served,) = (await node_row(http))["serving"]
    assert served["repo"] == REPO
    assert served["target_head"] == TARGET
    assert served["deploy_id"] == deploy_id


async def test_self_update_blocks_others_and_marks_the_disconnect(
    dashboard, http, nodes
):
    held = HeldDeployment()
    client = nodes.start(approval_handler=held.handle, reconnect_base=1.0)
    await connect(dashboard, client)

    self_id = await announce(
        dashboard, client, SELF_REPO, SELF_TARGET, is_self_update=True
    )
    other_id = await announce(dashboard, client, REPO, TARGET)
    assert (await pending_row(http, self_id))[
        "apply_policy"
    ] is None  # 옛 노드처럼 칸 없음

    approved = await http.post(
        "/api/orch/approve", json={"deploy_id": self_id, "approved_by": "contract"}
    )
    assert approved.status_code == 200, approved.text
    await held.wait_entered()

    blocked = await http.post("/api/orch/approve", json={"deploy_id": other_id})
    assert blocked.status_code == 409
    assert blocked.json()["error"] == (
        f"self-update {self_id} is deploying on node {NODE_ID}; "
        "approve other deployments after it finishes"
    )
    assert blocked.json()["blocking_self_update"]["status"] == "deploying"

    socket, loop = client._ws, client._loop
    await asyncio.wrap_future(
        asyncio.run_coroutine_threadsafe(
            socket.close(code=1012, reason="contract: self-update restart"), loop
        )
    )
    gone = await dashboard.wait_for(
        lambda e: e["type"] == "node_disconnected" and e["node_id"] == NODE_ID
    )
    assert gone["self_update_deploying"] is True
    assert gone["self_update_deploy_id"] == self_id
    node = await node_row(http)
    assert node["connected"] == 0
    assert node["self_update_deploying"] is True
    assert node["self_update_deploy_id"] == self_id

    held.release.set()
    await dashboard.wait_for(
        lambda e: (
            e["type"] == "status_change"
            and e["deploy_id"] == self_id
            and e["status"] == "success"
        ),
        timeout=15,
    )
    node = await node_row(http)
    assert node["self_update_deploying"] is False
    assert {row["repo"] for row in node["serving"]} == {SELF_REPO}


async def test_failed_rollback_reports_stage_and_recovery_in_history(
    dashboard, http, nodes
):
    def failing_rollback(approval: dict, progress) -> None:
        progress("build")
        progress("starting")
        progress("recovering")
        raise RuntimeError("deployment failed: services did not start")

    client = nodes.start(approval_handler=failing_rollback)
    await connect(dashboard, client)

    deploy_id = await announce(dashboard, client, REPO, TARGET)
    response = await http.post(
        "/api/orch/approve", json={"deploy_id": deploy_id, "approved_by": "contract"}
    )
    assert response.status_code == 200, response.text
    await dashboard.wait_for(
        lambda e: (
            e["type"] == "deploy_progress"
            and e["deploy_id"] == deploy_id
            and e["stage"] == "recovering"
        ),
        timeout=15,
    )
    await dashboard.wait_for(
        lambda e: (
            e["type"] == "status_change"
            and e["deploy_id"] == deploy_id
            and e["status"] in {"failed", "pending"}
        ),
        timeout=15,
    )

    rows = (await http.get("/api/orch/history")).json()["deploys"]
    failed = next(
        row
        for row in rows
        if row["deploy_id"].startswith("attempt:")
        and row["canonical_deploy_id"] == deploy_id
    )
    assert failed["failed_stage"] == "starting"
    assert failed["recovery_attempted"] is True
