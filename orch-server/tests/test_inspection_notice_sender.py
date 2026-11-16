"""점검 필요 알림 보내기 시험(카드 #1260).

가짜 HTTP 받는 쪽으로 실제 저장소의 원장 줄을 보낸다. 노드별 묶기, 고정 delivery_id, 2xx 판정,
outcome unknown, 실패 뒤 간격 증가, 설정 없음, 루프 예외 생존, 토큰 비노출을 본다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from haniel_orch.api import create_api_routes
from haniel_orch.event_store import EventStore
from haniel_orch.hub import WebSocketHub
from haniel_orch.inspection_notice_sender import (
    InspectionNoticeSender,
    compose_notice_message,
    delivery_id_for,
)
from haniel_orch.node_registry import NodeRegistry
from haniel_orch.statechart.inspection_notice_text import INSTRUCTION_TEXT

import state_support as support

CLOSING_DATA = {
    "failed_attempt_id": "a1",
    "stable_source": "a1",
    "marker_created_at": None,
}
TOKEN = "tok-SECRET-123"


class FakeSoulstream:
    """받는 쪽. 상태 코드와 본문, 연결 오류를 시험이 고른다."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.payload: dict = {"outcome": "queued"}
        self.error: Exception | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, json=self.payload)

    def payloads(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]


@pytest.fixture
async def fake_stream():
    fake = FakeSoulstream()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    yield fake, client
    await client.aclose()


def make_sender(
    store: EventStore, client: httpx.AsyncClient, **overrides
) -> InspectionNoticeSender:
    options = {
        "url": "http://soulstream.test",
        "token": TOKEN,
        "session_id": "sess-1",
        "client": client,
        "poll_interval_sec": 60.0,
        "retry_base_sec": 60.0,
        "retry_cap_sec": 3600.0,
    }
    options.update(overrides)
    return InspectionNoticeSender(store, **options)


async def locked_request(
    store: EventStore, deploy_id: str = "n1:r:main:aaa", node_id: str = "n1"
) -> None:
    await store.create_deploy_event(
        deploy_id=deploy_id,
        node_id=node_id,
        repo="r",
        branch="main",
        commits=["c"],
        affected_services=[],
        diff_stat=None,
        detected_at="2026-08-01T00:00:00Z",
        target_head="target",
    )
    await support.fire(store, deploy_id, "plan_failed_journal", data=CLOSING_DATA)


async def notice_rows(store: EventStore) -> list[dict]:
    cursor = await store._db.execute(
        "SELECT * FROM inspection_notices ORDER BY created_at, rowid"
    )
    names = [description[0] for description in cursor.description]
    return [dict(zip(names, row)) for row in await cursor.fetchall()]


async def make_due(store: EventStore, notice_id: str) -> None:
    await store._db.execute(
        "UPDATE inspection_notices SET next_attempt_at = ? WHERE notice_id = ?",
        ("2000-01-01T00:00:00+00:00", notice_id),
    )
    await store._db.commit()


# ---------------------------------------------------------------- 보내기


async def test_sends_a_node_bundle_once_and_marks_it_sent(store: EventStore, fake_stream):
    fake, client = fake_stream
    await locked_request(store, "n1:r:main:a", "n1")
    await locked_request(store, "n1:r:main:b", "n1")
    sender = make_sender(store, client)

    await sender.send_pass()

    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert request.url.path == "/api/sessions/sess-1/intervene"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    payload = fake.payloads()[0]
    assert set(payload) == {"text", "delivery_id"}
    assert payload["text"].startswith(INSTRUCTION_TEXT)
    assert payload["text"].count("node_id: n1") == 2
    assert "n1:r:main:a" in payload["text"] and "n1:r:main:b" in payload["text"]
    assert payload["delivery_id"].startswith("haniel-inspection-")

    rows = await notice_rows(store)
    assert [row["state"] for row in rows] == ["sent", "sent"]
    assert all(row["sent_at"] for row in rows)

    # 보낸 줄은 다시 보내지 않는다
    await sender.send_pass()
    assert len(fake.requests) == 1


async def test_bundles_are_per_node(store: EventStore, fake_stream):
    fake, client = fake_stream
    await locked_request(store, "n1:r:main:a", "n1")
    await locked_request(store, "n1:r:main:b", "n1")
    await locked_request(store, "n2:r:main:c", "n2")

    await make_sender(store, client).send_pass()

    assert len(fake.requests) == 2
    texts = [payload["text"] for payload in fake.payloads()]
    n1_text = next(text for text in texts if "n1:r:main:a" in text)
    assert "n1:r:main:b" in n1_text
    assert "n2:r:main:c" not in n1_text


async def test_delivery_id_is_fixed_for_the_same_bundle(store: EventStore, fake_stream):
    fake, client = fake_stream
    fake.status = 500
    await locked_request(store)
    sender = make_sender(store, client)

    await sender.send_pass()
    first = fake.payloads()[0]["delivery_id"]
    first_text = fake.payloads()[0]["text"]
    row = (await notice_rows(store))[0]
    await make_due(store, row["notice_id"])
    await sender.send_pass()

    second = fake.payloads()[1]
    assert second["delivery_id"] == first
    assert second["text"] == first_text


def test_delivery_id_ignores_the_order_of_notice_ids():
    assert delivery_id_for(["b", "a"]) == delivery_id_for(["a", "b"])


# ---------------------------------------------------------------- 실패와 재시도


async def test_failure_keeps_the_notice_unsent_and_backs_off(store: EventStore, fake_stream):
    fake, client = fake_stream
    fake.status = 500
    fake.payload = {"detail": "down"}
    await locked_request(store)
    sender = make_sender(store, client)

    await sender.send_pass()

    row = (await notice_rows(store))[0]
    assert row["state"] == "unsent"
    assert row["attempts"] == 1
    assert row["last_error"].startswith("HTTP 500")
    due = datetime.fromisoformat(row["next_attempt_at"])
    delay = (due - datetime.now(timezone.utc)).total_seconds()
    assert 55 <= delay <= 65

    # 아직 다음 차례가 아니라 다시 보내지 않는다
    await sender.send_pass()
    assert len(fake.requests) == 1

    await make_due(store, row["notice_id"])
    await sender.send_pass()

    row = (await notice_rows(store))[0]
    assert row["attempts"] == 2
    due = datetime.fromisoformat(row["next_attempt_at"])
    delay = (due - datetime.now(timezone.utc)).total_seconds()
    assert 115 <= delay <= 125


async def test_outcome_unknown_is_not_marked_sent(store: EventStore, fake_stream):
    fake, client = fake_stream
    fake.payload = {"outcome": "unknown"}
    await locked_request(store)

    await make_sender(store, client).send_pass()

    row = (await notice_rows(store))[0]
    assert row["state"] == "unsent"
    assert row["attempts"] == 1
    assert "outcome unknown" in row["last_error"]
    assert row["sent_at"] is None


async def test_connection_error_keeps_the_deploy_unchanged(store: EventStore, fake_stream):
    fake, client = fake_stream
    fake.error = httpx.ConnectError("boom")
    await locked_request(store)

    await make_sender(store, client).send_pass()

    row = (await notice_rows(store))[0]
    assert row["state"] == "unsent" and row["attempts"] == 1
    assert row["last_error"].startswith("ConnectError")
    cursor = await store._db.execute(
        "SELECT status, sm_status, sm_lock FROM deploy_events WHERE deploy_id = ?",
        ("n1:r:main:aaa",),
    )
    assert tuple(await cursor.fetchone()) == ("pending", "retry", "l_on")


async def test_4xx_is_logged_as_a_warning_without_the_token(
    store: EventStore, fake_stream, caplog
):
    fake, client = fake_stream
    fake.status = 400
    fake.payload = {"detail": f"bad token {TOKEN}"}
    await locked_request(store)

    with caplog.at_level(logging.WARNING):
        await make_sender(store, client).send_pass()

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert any("HTTP 400" in record.getMessage() for record in warnings)
    assert TOKEN not in caplog.text
    row = (await notice_rows(store))[0]
    assert TOKEN not in row["last_error"]
    assert "***" in row["last_error"]


# ---------------------------------------------------------------- 설정 없음


async def test_missing_settings_warns_once_and_keeps_the_ledger(
    store: EventStore, caplog
):
    await locked_request(store)
    sender = InspectionNoticeSender(store, url=None, token=None, session_id="s")

    with caplog.at_level(logging.WARNING):
        sender.start()
        sender.start()

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "ORCH_SOULSTREAM_URL" in message
    assert "ORCH_SOULSTREAM_TOKEN" in message
    assert sender._task is None
    assert len(await notice_rows(store)) == 1

    await sender.stop()


async def test_configured_sender_starts_a_loop(store: EventStore, fake_stream):
    _, client = fake_stream
    sender = make_sender(store, client, poll_interval_sec=0.01)
    sender.start()
    try:
        assert sender._task is not None
    finally:
        await sender.stop()
    assert sender._task is None


# ---------------------------------------------------------------- 루프 생존


async def test_send_loop_survives_a_failed_pass(store: EventStore, fake_stream):
    _, client = fake_stream
    sender = make_sender(store, client, poll_interval_sec=0.01)
    calls: list[int] = []
    original = sender.send_pass

    async def flaky() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        await original()

    sender.send_pass = flaky  # type: ignore[method-assign]
    sender.start()
    try:
        for _ in range(300):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(calls) >= 2
    finally:
        await sender.stop()


# ---------------------------------------------------------------- 대기 목록 반영


async def test_pending_api_shows_sent_state_after_delivery(
    store: EventStore, fake_stream
):
    fake, client = fake_stream
    await locked_request(store)
    await make_sender(store, client).send_pass()

    hub = WebSocketHub(NodeRegistry(store), store, token="t")
    api = TestClient(Starlette(routes=create_api_routes(hub, store)))
    notice = api.get("/api/orch/pending").json()["deploys"][0]["inspection_notice"]

    assert notice["state"] == "sent"
    assert notice["sent_at"] is not None


def test_compose_puts_the_instruction_first():
    message = compose_notice_message(["node_id: n", "node_id: m"])
    assert message.startswith(INSTRUCTION_TEXT)
    assert message.index("node_id: n") < message.index("node_id: m")
