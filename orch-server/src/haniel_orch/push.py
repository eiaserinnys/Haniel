"""Push notification service — relay mode sends to CF Workers, null mode is no-op."""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)


@runtime_checkable
class PushService(Protocol):
    """Push notification service interface."""

    async def notify(self, title: str, body: str, data: dict[str, Any]) -> None: ...
    async def close(self) -> None: ...


class RelayPushService:
    """Sends push notifications via CF Workers relay server."""

    def __init__(
        self,
        relay_url: str,
        instance_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = relay_url.rstrip("/")
        self._key = instance_key
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def notify(self, title: str, body: str, data: dict[str, Any]) -> None:
        """Send push via relay. Raises on network/HTTP error — caller handles."""
        resp = await self._client.post(
            f"{self._url}/v1/push",
            headers={"Authorization": f"Bearer {self._key}"},
            json={"title": title, "body": body, "data": data},
        )
        if not resp.is_success:
            logger.warning(f"Push relay returned {resp.status_code}: {resp.text}")
        else:
            logger.debug(f"Push sent: {resp.json()}")

    async def close(self) -> None:
        await self._client.aclose()


class NullPushService:
    """No-op push service when push is not configured."""

    async def notify(self, title: str, body: str, data: dict[str, Any]) -> None:
        pass

    async def close(self) -> None:
        pass
