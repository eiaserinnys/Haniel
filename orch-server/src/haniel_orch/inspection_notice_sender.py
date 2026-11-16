"""점검 필요 잠금 알림을 소울스트림 세션으로 보낸다(카드 #1260).

원장에 쌓인 보내지 않은 줄을 노드별로 묶어 한 메시지로 보낸다. 같은 묶음의 delivery_id는 묶인 줄 id로
정해지므로 재전송해도 받는 쪽이 같은 전달로 안다. 설정이 하나라도 없으면 보내지 않고 기동 때 경고
한 줄만 남긴다(원장 줄은 계속 쌓인다). 보내기 실패는 요청 상태와 잠금, 배포 흐름을 바꾸지 않는다.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any
from urllib.parse import quote

import httpx

from .statechart.inspection_notice import (
    register_wake_listener,
    unregister_wake_listener,
)
from .statechart.inspection_notice_text import compose_notice_message, redact_secrets

logger = logging.getLogger(__name__)

SETTING_URL = "ORCH_SOULSTREAM_URL"
SETTING_TOKEN = "ORCH_SOULSTREAM_TOKEN"
SETTING_SESSION_ID = "ORCH_INSPECTION_SESSION_ID"

DELIVERY_ID_PREFIX = "haniel-inspection-"
OUTCOME_UNKNOWN = "unknown"
ERROR_LIMIT = 500


def delivery_id_for(notice_ids: list[str]) -> str:
    """묶인 줄 id로 정해지는 고정 값. 같은 묶음이면 언제 보내도 같은 값이다."""
    digest = hashlib.sha256("\n".join(sorted(notice_ids)).encode("utf-8")).hexdigest()
    return f"{DELIVERY_ID_PREFIX}{digest[:32]}"


class InspectionNoticeSender:
    """보내기 루프 하나. 커밋 직후 깨우고, 그 밖에는 일정 간격으로 보낼 줄을 찾는다."""

    def __init__(
        self,
        store: Any,
        *,
        url: str | None,
        token: str | None,
        session_id: str | None,
        client: httpx.AsyncClient | None = None,
        poll_interval_sec: float = 60.0,
        retry_base_sec: float = 60.0,
        retry_cap_sec: float = 3600.0,
        timeout_sec: float = 10.0,
    ) -> None:
        self._store = store
        self._url = url or None
        self._token = token or None
        self._session_id = session_id or None
        self._client = client
        self._injected_client = client
        self._owns_client = False
        self._poll_interval_sec = poll_interval_sec
        self._retry_base_sec = retry_base_sec
        self._retry_cap_sec = retry_cap_sec
        self._timeout_sec = timeout_sec
        self._task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._wake_callback: Any = None
        self._started = False

    def missing_settings(self) -> list[str]:
        missing = []
        if not self._url:
            missing.append(SETTING_URL)
        if not self._token:
            missing.append(SETTING_TOKEN)
        if not self._session_id:
            missing.append(SETTING_SESSION_ID)
        return missing

    @property
    def configured(self) -> bool:
        return not self.missing_settings()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        missing = self.missing_settings()
        if missing:
            logger.warning(
                "Inspection notice sending disabled: missing %s; notices stay in the ledger",
                ", ".join(missing),
            )
            return
        self._wake = asyncio.Event()
        self._wake_callback = self._wake.set
        register_wake_listener(self._wake_callback)
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout_sec)
            self._owns_client = True
        self._task = asyncio.create_task(self._run(), name="inspection-notice-sender")

    async def stop(self) -> None:
        if self._wake_callback is not None:
            unregister_wake_listener(self._wake_callback)
            self._wake_callback = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
            self._owns_client = False
        self._started = False

    async def _run(self) -> None:
        while True:
            await self._send_pass_safely()
            await self._wait_for_wake()

    async def _wait_for_wake(self) -> None:
        assert self._wake is not None
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval_sec)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def _send_pass_safely(self) -> None:
        """보내기 한 바퀴의 예외가 루프를 멈추지 않게 한다."""
        try:
            await self.send_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Inspection notice send pass failed")

    async def send_pass(self) -> None:
        bundles = await self._store.due_inspection_notice_bundles()
        for group in bundles:
            notice_ids = [row["notice_id"] for row in group]
            text = compose_notice_message(group)
            sent, error = await self._post(text, delivery_id_for(notice_ids))
            if sent:
                await self._store.mark_inspection_notices_sent(notice_ids)
            else:
                await self._store.note_inspection_notice_failures(
                    notice_ids,
                    error=error or "unknown error",
                    base_sec=self._retry_base_sec,
                    cap_sec=self._retry_cap_sec,
                )

    def _intervene_url(self) -> str:
        assert self._url is not None and self._session_id is not None
        session = quote(self._session_id, safe="")
        return f"{self._url.rstrip('/')}/api/sessions/{session}/intervene"

    async def _post(self, text: str, delivery_id: str) -> tuple[bool, str | None]:
        assert self._client is not None and self._token is not None
        try:
            response = await self._client.post(
                self._intervene_url(),
                json={"text": text, "delivery_id": delivery_id},
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except Exception as exc:
            error = self._redacted_error(f"{type(exc).__name__}: {exc}")
            logger.info("Inspection notice delivery failed: %s", error)
            return False, error
        status = response.status_code
        if not 200 <= status < 300:
            error = self._redacted_error(f"HTTP {status}: {response.text}")
            if 400 <= status < 500:
                logger.warning(
                    "Inspection notice rejected by soulstream (HTTP %s): %s", status, error
                )
            else:
                logger.info("Inspection notice delivery failed: %s", error)
            return False, error
        try:
            payload = response.json()
        except ValueError:
            logger.warning(
                "Inspection notice accepted without a JSON body (HTTP %s)", status
            )
            return True, None
        outcome = payload.get("outcome") if isinstance(payload, dict) else None
        if outcome is None:
            logger.warning(
                "Inspection notice accepted without an outcome (HTTP %s)", status
            )
            return True, None
        if outcome == OUTCOME_UNKNOWN:
            error = f"HTTP {status}: outcome {OUTCOME_UNKNOWN}"
            logger.info("Inspection notice delivery failed: %s", error)
            return False, error
        return True, None

    def _redacted_error(self, error: str) -> str:
        token = self._token or ""
        return redact_secrets(error, extra=(token,))[:ERROR_LIMIT]
