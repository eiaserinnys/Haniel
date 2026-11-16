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
        self.payload: object = {"outcome": "queued"}
        self.json_body = True
        self.error: Exception | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if not self.json_body:
            return httpx.Response(self.status)
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
    store: EventStore,
    deploy_id: str = "n1:r:main:aaa",
    node_id: str = "n1",
    *,
    branch: str = "main",
) -> None:
    await store.create_deploy_event(
        deploy_id=deploy_id,
        node_id=node_id,
        repo="r",
        branch=branch,
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
    text = payload["text"]
    assert text.splitlines()[0] == "[하니엘 점검 필요] n1 2건: r 외 1"
    assert text.count(INSTRUCTION_TEXT) == 1
    assert text.count("node_id: n1") == 2
    assert "n1:r:main:a" in text and "n1:r:main:b" in text
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
    assert first_text.splitlines()[0] == "[하니엘 점검 필요] n1 1건: r"
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
    await locked_request(store, "n1:r:main:a", "n1")
    await locked_request(store, "n1:r:dev:b", "n1", branch="dev")
    sender = make_sender(store, client)

    await sender.send_pass()

    rows = await notice_rows(store)
    assert [row["state"] for row in rows] == ["unsent", "unsent"]
    # 한 묶음의 줄은 같은 시도 수와 같은 다음 시각을 갖는다(노드 단위 간격)
    assert [row["attempts"] for row in rows] == [1, 1]
    assert len({row["next_attempt_at"] for row in rows}) == 1
    assert all(row["last_error"].startswith("HTTP 500") for row in rows)
    due = datetime.fromisoformat(rows[0]["next_attempt_at"])
    delay = (due - datetime.now(timezone.utc)).total_seconds()
    assert 55 <= delay <= 65

    # 아직 다음 차례가 아니라 다시 보내지 않는다
    await sender.send_pass()
    assert len(fake.requests) == 1

    await make_due(store, rows[0]["notice_id"])
    await sender.send_pass()

    rows = await notice_rows(store)
    assert [row["attempts"] for row in rows] == [2, 2]
    assert len({row["next_attempt_at"] for row in rows}) == 1
    due = datetime.fromisoformat(rows[0]["next_attempt_at"])
    delay = (due - datetime.now(timezone.utc)).total_seconds()
    assert 115 <= delay <= 125


async def test_a_new_row_makes_the_whole_node_bundle_due(store: EventStore, fake_stream):
    fake, client = fake_stream
    await locked_request(store, "n1:r:main:a", "n1")
    sender = make_sender(store, client)

    fake.status = 500
    await sender.send_pass()
    assert (await notice_rows(store))[0]["attempts"] == 1

    # 새 줄은 다음 시각이 없어 그 노드를 곧바로 보낼 차례로 만든다. 기다리던 줄까지 한 묶음으로 나간다
    fake.status = 200
    await locked_request(store, "n1:r:dev:b", "n1", branch="dev")
    await sender.send_pass()

    assert len(fake.requests) == 2
    payload = fake.payloads()[1]
    assert "n1:r:main:a" in payload["text"]
    assert "n1:r:dev:b" in payload["text"]
    assert [row["state"] for row in await notice_rows(store)] == ["sent", "sent"]


def test_retry_delay_is_capped_without_overflow():
    from haniel_orch.statechart.inspection_notice import retry_delay_sec

    assert retry_delay_sec(1, 60.0, 3600.0) == 60.0
    assert retry_delay_sec(2, 60.0, 3600.0) == 120.0
    # 시도 수가 아주 커져도 지수 폭발과 float 넘침 없이 상한에서 멈춘다
    assert retry_delay_sec(10**9, 60.0, 3600.0) == 3600.0


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


async def test_2xx_without_a_body_is_treated_as_sent(
    store: EventStore, fake_stream, caplog
):
    fake, client = fake_stream
    fake.status = 204
    fake.json_body = False
    await locked_request(store)

    with caplog.at_level(logging.WARNING):
        await make_sender(store, client).send_pass()

    row = (await notice_rows(store))[0]
    assert row["state"] == "sent" and row["sent_at"]
    assert "without a JSON body" in caplog.text


async def test_2xx_without_an_outcome_is_treated_as_sent(
    store: EventStore, fake_stream, caplog
):
    fake, client = fake_stream
    fake.payload = {"queued": True}
    await locked_request(store)

    with caplog.at_level(logging.WARNING):
        await make_sender(store, client).send_pass()

    row = (await notice_rows(store))[0]
    assert row["state"] == "sent" and row["sent_at"]
    assert "without an outcome" in caplog.text


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


# ---------------------------------------------------------------- 깨움에서 전달까지


async def test_lock_transition_wakes_the_sender_and_delivers_once(
    store: EventStore, fake_stream
):
    fake, client = fake_stream
    sender = make_sender(store, client, poll_interval_sec=60.0)
    sender.start()
    try:
        # 잠금 전이가 커밋되고 깨우기가 보내기 루프를 깨운다. 가짜 받는 쪽이 한 번 받는다
        await locked_request(store)
        for _ in range(300):
            if fake.requests:
                break
            await asyncio.sleep(0.01)
        assert len(fake.requests) == 1
        assert "n1:r:main:aaa" in fake.payloads()[0]["text"]
        assert [row["state"] for row in await notice_rows(store)] == ["sent"]
        await asyncio.sleep(0.05)
        assert len(fake.requests) == 1
    finally:
        await sender.stop()


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


def compose_rows(*repos: str, node_id: str = "n1") -> list[dict]:
    return [
        {"node_id": node_id, "repo": repo, "text": f"node_id: {node_id}\nrepo: {repo}"}
        for repo in repos
    ]


def test_the_first_line_is_the_header_and_the_bundle_summary():
    message = compose_notice_message(compose_rows("r"))
    lines = message.splitlines()

    assert lines[0] == "[하니엘 점검 필요] n1 1건: r"
    assert "외" not in lines[0]
    assert message.startswith(lines[0] + "\n")
    assert message.index(INSTRUCTION_TEXT) > len(lines[0])


def test_the_summary_names_the_first_repo_and_the_rest_of_the_bundle():
    message = compose_notice_message(
        compose_rows(
            "seosoyoung-workspace", "a", "b", "c", "d", node_id="silent-manari-ts"
        )
    )

    assert message.splitlines()[0] == (
        "[하니엘 점검 필요] silent-manari-ts 5건: seosoyoung-workspace 외 4"
    )


def test_the_instruction_appears_once_after_the_first_line():
    message = compose_notice_message(compose_rows("r", "s"))

    assert message.count(INSTRUCTION_TEXT) == 1
    assert not message.startswith(INSTRUCTION_TEXT)
    assert message.index("node_id: n1") > message.index(INSTRUCTION_TEXT)


def test_the_same_rows_compose_the_same_message():
    rows = compose_rows("r", "s")

    assert compose_notice_message(rows) == compose_notice_message(rows)


def test_the_instruction_hands_the_check_to_a_card_owner():
    assert "현 상태를 스스로 판단하지 않고" in INSTRUCTION_TEXT
    assert "점검 카드 담당이 첫 일로 확인한다" in INSTRUCTION_TEXT
    assert "요청이 이미 닫혔거나 더 새 요청이 있으면 현 상태부터 확인한다" not in INSTRUCTION_TEXT
