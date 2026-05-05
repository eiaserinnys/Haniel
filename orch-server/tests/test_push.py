"""Tests for push notification module."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from haniel_orch.push import NullPushService, PushService, RelayPushService


class TestRelayPushService:
    async def test_notify_sends_post(self):
        """notify() sends POST to relay URL with correct payload."""
        mock_response = MagicMock(is_success=True, json=lambda: {"sent": 1})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        svc = RelayPushService("https://relay.example.com", "inst_key", client=mock_client)

        await svc.notify("Deploy", "New deploy", {"type": "new_pending"})

        mock_client.post.assert_called_once_with(
            "https://relay.example.com/v1/push",
            headers={"Authorization": "Bearer inst_key"},
            json={"title": "Deploy", "body": "New deploy", "data": {"type": "new_pending"}},
        )

    async def test_notify_strips_trailing_slash(self):
        """relay_url trailing slash is stripped."""
        mock_response = MagicMock(is_success=True, json=lambda: {"sent": 0})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        svc = RelayPushService("https://relay.example.com/", "key", client=mock_client)

        await svc.notify("t", "b", {})

        call_url = mock_client.post.call_args[0][0]
        assert call_url == "https://relay.example.com/v1/push"

    async def test_notify_does_not_catch_exceptions(self):
        """Network errors propagate to caller (_fire_push handles them)."""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("connection refused"))

        svc = RelayPushService("https://relay.example.com", "key", client=mock_client)

        with pytest.raises(Exception, match="connection refused"):
            await svc.notify("t", "b", {})

    async def test_notify_logs_on_http_error(self):
        """HTTP error response is logged but does not raise."""
        mock_response = MagicMock(
            is_success=False, status_code=500, text="Internal Server Error"
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        svc = RelayPushService("https://relay.example.com", "key", client=mock_client)

        # Should not raise — HTTP errors are logged but not exceptions
        await svc.notify("t", "b", {})

    async def test_close_closes_client(self):
        """close() calls aclose on httpx client."""
        mock_client = AsyncMock()

        svc = RelayPushService("https://relay.example.com", "key", client=mock_client)

        await svc.close()
        mock_client.aclose.assert_called_once()


class TestNullPushService:
    async def test_notify_noop(self):
        """NullPushService.notify() does nothing."""
        svc = NullPushService()
        await svc.notify("t", "b", {})  # No exception

    async def test_close_noop(self):
        """NullPushService.close() does nothing."""
        svc = NullPushService()
        await svc.close()  # No exception


class TestPushServiceProtocol:
    def test_relay_implements_protocol(self):
        """RelayPushService satisfies PushService protocol."""
        svc = RelayPushService("http://localhost", "k")
        assert isinstance(svc, PushService)

    def test_null_implements_protocol(self):
        """NullPushService satisfies PushService protocol."""
        svc = NullPushService()
        assert isinstance(svc, PushService)
