"""허브가 새 글을 받고 보내는 길(카드 #1259 PR 2b-2): 받았다 분기, hello 대조, 노드 스스로 한 배포 보고의 「종결을 받았다」,
옛 노드의 ignored 길, 모르는 글은 경고 뒤 버림."""

import json
from unittest.mock import AsyncMock

from starlette.websockets import WebSocketDisconnect

from test_belief_flow import begin_via_probe
from test_inspection_lock import future_deadline, node_report, raw
from haniel_orch.event_store import EventStore
from haniel_orch.hub import WebSocketHub
from haniel_orch.node_registry import NodeRegistry
from haniel_orch.protocol import DeployAttemptReceived, HeldAttempt, NodeHello

DEPLOY_ID = "n1:r:main:target"


def hello(protocol: int | None = 1, held: list[HeldAttempt] | None = None) -> NodeHello:
    return NodeHello(
        node_id="n1", token="test-token", hostname="h", os="Linux", arch="x86_64", haniel_version="0.1.0",
        deploy_protocol=protocol, held_attempts=held,
    )


async def seed(store: EventStore) -> None:
    await store.create_deploy_event(
        deploy_id=DEPLOY_ID, node_id="n1", repo="r", branch="main", commits=["c"], affected_services=[], diff_stat=None,
        detected_at="2026-08-01T00:00:00Z", target_head="target",
    )


async def begin(store: EventStore, attempt_id: str, generation: str, protocol: int | None = 1) -> None:
    await store.attempts.begin_normal_attempt(
        orchestrator_attempt_id=attempt_id, deploy_id=DEPLOY_ID, connection_generation=generation, current_generation=generation,
        source="manual_single", approved_by="d", deadline_at=future_deadline(), node_protocol=protocol,
    )


def sent_types(websocket: AsyncMock) -> list[dict]:
    return [json.loads(call.args[0]) for call in websocket.send_text.await_args_list]


async def run_session(hub: WebSocketHub, messages: list) -> AsyncMock:
    websocket = AsyncMock()
    websocket.receive_text.side_effect = [m if isinstance(m, str) else m.model_dump_json() for m in messages] + [WebSocketDisconnect(code=1000)]
    await hub.handle_node_ws(websocket)
    return websocket


async def test_received_is_dispatched_and_the_connection_protocol_is_stored(store: EventStore):
    registry = NodeRegistry(store)
    hub = WebSocketHub(registry, store, token="test-token")
    await seed(store)
    # 먼저 붙어 시도를 연다(세대를 알아야 한다). 두 번째 세션이 받았다를 보낸다
    first = await run_session(hub, [hello(1, [])])
    assert first.send_text.await_count == 0
    generation = (await store.get_node_connection_identity("n1"))[0]
    assert (await store.get_nodes())[0]["deploy_protocol"] == 1
    await begin(store, "a1", generation)
    # hello에 목록이 없으면(None) 대조하지 않으므로 받았다만 믿음을 옮긴다. 세션이 끝나며 끊김을 알아 유예 마감이 걸린다
    await run_session(hub, [hello(1, None), DeployAttemptReceived(node_id="n1", deploy_id=DEPLOY_ID, orchestrator_attempt_id="a1", connection_generation=generation)])
    assert (await raw(store, "SELECT sm_belief FROM deploy_events"))[0][0] == "b_queued"
    assert (await raw(store, "SELECT deadline_kind FROM deploy_attempts"))[0][0] == "grace"


async def test_received_from_another_node_connection_is_dropped(store: EventStore, caplog):
    registry = NodeRegistry(store)
    hub = WebSocketHub(registry, store, token="test-token")
    await seed(store)
    await run_session(hub, [hello(1, [])])
    generation = (await store.get_node_connection_identity("n1"))[0]
    await begin(store, "a1", generation)
    with caplog.at_level("WARNING"):
        await run_session(hub, [hello(1, None), DeployAttemptReceived(node_id="other", deploy_id=DEPLOY_ID, orchestrator_attempt_id="a1", connection_generation=generation)])
    assert (await raw(store, "SELECT sm_belief FROM deploy_events"))[0][0] == "b_unknown"
    assert "connection identity mismatch" in caplog.text


async def test_hello_reconciliation_runs_before_the_message_loop_and_logs(store: EventStore, caplog):
    registry = NodeRegistry(store)
    hub = WebSocketHub(registry, store, token="test-token")
    await seed(store)
    await run_session(hub, [hello(1, [])])
    generation = (await store.get_node_connection_identity("n1"))[0]
    await begin(store, "a1", generation)
    with caplog.at_level("INFO"):
        await run_session(hub, [hello(1, [])])  # 목록은 있으나 비었다: 닿았는지 모름이 실행 여부 불명으로 닫힌다
    assert (await raw(store, "SELECT status, sm_status, sm_belief, sm_lock FROM deploy_events"))[0] == ("pending", "again", "b_none", "l_off")
    assert "hello reconcile" in caplog.text and "missing" in caplog.text
    # 옛 노드(목록 없음)는 대조하지 않는다
    await begin_via_probe(store, "a2", protocol=None, generation=(await store.get_node_connection_identity("n1"))[0] or generation, deploy_id=DEPLOY_ID)
    await run_session(hub, [hello(None, None)])
    assert (await raw(store, "SELECT outcome FROM deploy_attempts WHERE orchestrator_attempt_id = 'a2'"))[0][0] == "active"


async def test_self_deploy_terminal_reports_are_always_answered_on_protocol_one_connections(store: EventStore):
    registry = NodeRegistry(store)
    hub = WebSocketHub(registry, store, token="test-token")
    # 요청 줄이 없을 때 온 실패(G12)에도 답한다(closed_now). 같은 글이 또 오면 already_closed
    websocket = await run_session(hub, [hello(1, []), node_report("failed", node_attempt_id="na1", deploy_id=DEPLOY_ID, node_id="n1"), node_report("failed", node_attempt_id="na1", deploy_id=DEPLOY_ID, node_id="n1")])
    answers = [m for m in sent_types(websocket) if m["type"] == "deploy_terminal_received"]
    assert [(a["node_attempt_id"], a["outcome"], a["deploy_id"]) for a in answers] == [("na1", "closed_now", DEPLOY_ID), ("na1", "already_closed", DEPLOY_ID)]
    # 요청이 있으면 성공 보고가 요청을 닫고 closed_now, 시작 보고에는 답이 없다
    await seed(store)
    websocket = await run_session(hub, [hello(1, []), node_report("started", node_attempt_id="na2", deploy_id=DEPLOY_ID, node_id="n1"), node_report("succeeded", node_attempt_id="na2", deploy_id=DEPLOY_ID, node_id="n1")])
    answers = [m for m in sent_types(websocket) if m["type"] == "deploy_terminal_received"]
    assert [(a["node_attempt_id"], a["outcome"]) for a in answers] == [("na2", "closed_now")]
    assert (await store.get_deploy_event(DEPLOY_ID))["status"] == "success"


async def test_legacy_connections_get_no_terminal_received(store: EventStore):
    registry = NodeRegistry(store)
    hub = WebSocketHub(registry, store, token="test-token")
    await seed(store)
    websocket = await run_session(hub, [hello(None, None), node_report("failed", node_attempt_id="na1", deploy_id=DEPLOY_ID, node_id="n1")])
    assert [m["type"] for m in sent_types(websocket)] == []
    assert (await store.get_nodes())[0]["deploy_protocol"] is None


async def test_an_unknown_message_type_is_warned_and_dropped(store: EventStore, caplog):
    registry = NodeRegistry(store)
    hub = WebSocketHub(registry, store, token="test-token")
    with caplog.at_level("WARNING"):
        await run_session(hub, [hello(1, []), json.dumps({"type": "deploy_future_letter", "node_id": "n1"})])
    assert "Unknown message type" in caplog.text
