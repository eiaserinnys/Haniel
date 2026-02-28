"""Tests for haniel webhook notifications."""

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from haniel.webhook import (
    WebhookFormat,
    WebhookNotifier,
    WebhookMessage,
    EventType,
    format_slack_message,
    format_discord_message,
    format_json_message,
)


class TestWebhookFormat:
    """Tests for WebhookFormat enum."""

    def test_formats_exist(self):
        """Test that all required formats exist."""
        assert WebhookFormat.SLACK is not None
        assert WebhookFormat.DISCORD is not None
        assert WebhookFormat.JSON is not None

    def test_format_from_string(self):
        """Test creating format from string."""
        assert WebhookFormat("slack") == WebhookFormat.SLACK
        assert WebhookFormat("discord") == WebhookFormat.DISCORD
        assert WebhookFormat("json") == WebhookFormat.JSON


class TestEventType:
    """Tests for EventType enum."""

    def test_event_types_exist(self):
        """Test that all required event types exist."""
        assert EventType.SERVICE_STARTED is not None
        assert EventType.CHANGE_DETECTED is not None
        assert EventType.DEPLOYING is not None
        assert EventType.DEPLOY_COMPLETE is not None
        assert EventType.GRACEFUL_FAILED is not None
        assert EventType.CIRCUIT_BREAKER is not None


class TestWebhookMessage:
    """Tests for WebhookMessage dataclass."""

    def test_create_message(self):
        """Test creating a webhook message."""
        msg = WebhookMessage(
            event_type=EventType.SERVICE_STARTED,
            service_name="my-service",
            message="Service started successfully",
        )
        assert msg.event_type == EventType.SERVICE_STARTED
        assert msg.service_name == "my-service"
        assert msg.message == "Service started successfully"

    def test_message_with_details(self):
        """Test message with additional details."""
        msg = WebhookMessage(
            event_type=EventType.CHANGE_DETECTED,
            service_name="my-service",
            message="Changes detected",
            details={"commits": ["abc123", "def456"], "repo": "my-repo"},
        )
        assert msg.details["commits"] == ["abc123", "def456"]
        assert msg.details["repo"] == "my-repo"


class TestSlackFormat:
    """Tests for Slack Block Kit formatting."""

    def test_format_service_started(self):
        """Test Slack format for service started event."""
        msg = WebhookMessage(
            event_type=EventType.SERVICE_STARTED,
            service_name="my-service",
            message="Service started successfully",
        )
        payload = format_slack_message(msg)

        # Should be Block Kit format
        assert "blocks" in payload
        blocks = payload["blocks"]

        # Should have at least one section block
        assert len(blocks) >= 1

        # Find the main text
        text_found = False
        for block in blocks:
            if block.get("type") == "section":
                text = block.get("text", {}).get("text", "")
                if "my-service" in text:
                    text_found = True
        assert text_found

    def test_format_change_detected(self):
        """Test Slack format for change detected event."""
        msg = WebhookMessage(
            event_type=EventType.CHANGE_DETECTED,
            service_name="my-service",
            message="Changes detected in repository",
            details={
                "repo": "my-repo",
                "commits": ["abc123: Fix bug", "def456: Add feature"],
            },
        )
        payload = format_slack_message(msg)

        assert "blocks" in payload
        # Should include commit information
        json_str = json.dumps(payload)
        assert "abc123" in json_str or "commits" in json_str.lower()

    def test_format_circuit_breaker(self):
        """Test Slack format for circuit breaker event (should be warning)."""
        msg = WebhookMessage(
            event_type=EventType.CIRCUIT_BREAKER,
            service_name="my-service",
            message="Circuit breaker tripped after 5 failures",
            details={"failure_count": 5, "window": 300},
        )
        payload = format_slack_message(msg)

        # Circuit breaker should have warning styling
        assert "blocks" in payload
        json_str = json.dumps(payload)
        # Should indicate severity somehow
        assert "circuit" in json_str.lower() or "failure" in json_str.lower()

    def test_format_graceful_failed(self):
        """Test Slack format for graceful shutdown failed."""
        msg = WebhookMessage(
            event_type=EventType.GRACEFUL_FAILED,
            service_name="my-service",
            message="Graceful shutdown failed, killing process",
        )
        payload = format_slack_message(msg)
        assert "blocks" in payload


class TestDiscordFormat:
    """Tests for Discord webhook formatting."""

    def test_format_service_started(self):
        """Test Discord format for service started event."""
        msg = WebhookMessage(
            event_type=EventType.SERVICE_STARTED,
            service_name="my-service",
            message="Service started successfully",
        )
        payload = format_discord_message(msg)

        # Discord uses embeds
        assert "embeds" in payload
        embeds = payload["embeds"]
        assert len(embeds) >= 1

        embed = embeds[0]
        assert "title" in embed or "description" in embed

    def test_format_with_color(self):
        """Test Discord format includes appropriate color."""
        # Success event should have green color
        success_msg = WebhookMessage(
            event_type=EventType.DEPLOY_COMPLETE,
            service_name="my-service",
            message="Deploy complete",
        )
        success_payload = format_discord_message(success_msg)
        success_color = success_payload["embeds"][0].get("color")

        # Error event should have red color
        error_msg = WebhookMessage(
            event_type=EventType.CIRCUIT_BREAKER,
            service_name="my-service",
            message="Circuit breaker tripped",
        )
        error_payload = format_discord_message(error_msg)
        error_color = error_payload["embeds"][0].get("color")

        # Colors should be different (success vs error)
        assert success_color != error_color

    def test_format_with_fields(self):
        """Test Discord format with additional fields."""
        msg = WebhookMessage(
            event_type=EventType.CHANGE_DETECTED,
            service_name="my-service",
            message="Changes detected",
            details={"repo": "my-repo", "branch": "main"},
        )
        payload = format_discord_message(msg)

        embed = payload["embeds"][0]
        # Should have fields for details
        assert "fields" in embed or "description" in embed


class TestJsonFormat:
    """Tests for JSON webhook formatting."""

    def test_format_basic(self):
        """Test basic JSON format."""
        msg = WebhookMessage(
            event_type=EventType.SERVICE_STARTED,
            service_name="my-service",
            message="Service started",
        )
        payload = format_json_message(msg)

        assert payload["event"] == "service_started"
        assert payload["service"] == "my-service"
        assert payload["message"] == "Service started"
        assert "timestamp" in payload

    def test_format_with_details(self):
        """Test JSON format includes details."""
        msg = WebhookMessage(
            event_type=EventType.CHANGE_DETECTED,
            service_name="my-service",
            message="Changes detected",
            details={"repo": "my-repo", "commits": ["abc", "def"]},
        )
        payload = format_json_message(msg)

        assert payload["details"]["repo"] == "my-repo"
        assert payload["details"]["commits"] == ["abc", "def"]


class TestWebhookNotifier:
    """Tests for WebhookNotifier class."""

    def test_init_with_configs(self):
        """Test initializing notifier with webhook configs."""
        from haniel.config import WebhookConfig

        configs = [
            WebhookConfig(url="https://hooks.slack.com/xxx", format="slack"),
            WebhookConfig(url="https://discord.com/api/webhooks/xxx", format="discord"),
        ]
        notifier = WebhookNotifier(configs)
        assert len(notifier.webhooks) == 2

    def test_init_empty(self):
        """Test initializing notifier with no webhooks."""
        notifier = WebhookNotifier([])
        assert len(notifier.webhooks) == 0

    @pytest.mark.asyncio
    async def test_notify_sends_to_all_webhooks(self):
        """Test that notify sends to all configured webhooks."""
        from haniel.config import WebhookConfig

        configs = [
            WebhookConfig(url="https://hooks.slack.com/xxx", format="slack"),
            WebhookConfig(url="https://discord.com/api/webhooks/xxx", format="discord"),
        ]
        notifier = WebhookNotifier(configs)

        msg = WebhookMessage(
            event_type=EventType.SERVICE_STARTED,
            service_name="test",
            message="Test message",
        )

        with patch("haniel.webhook.aiohttp.ClientSession") as mock_session_class:
            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)

            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.__aenter__ = AsyncMock(return_value=mock_response)
            mock_response.__aexit__ = AsyncMock(return_value=None)

            mock_session.post = MagicMock(return_value=mock_response)
            mock_session_class.return_value = mock_session

            await notifier.notify(msg)

            # Should have posted to both webhooks
            assert mock_session.post.call_count == 2

    @pytest.mark.asyncio
    async def test_notify_handles_failure_gracefully(self):
        """Test that notify handles webhook failures gracefully."""
        from haniel.config import WebhookConfig

        configs = [
            WebhookConfig(url="https://hooks.slack.com/xxx", format="slack"),
        ]
        notifier = WebhookNotifier(configs)

        msg = WebhookMessage(
            event_type=EventType.SERVICE_STARTED,
            service_name="test",
            message="Test message",
        )

        with patch("haniel.webhook.aiohttp.ClientSession") as mock_session_class:
            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)

            # Simulate failure
            mock_session.post = MagicMock(side_effect=Exception("Network error"))
            mock_session_class.return_value = mock_session

            # Should not raise
            await notifier.notify(msg)

    def test_notify_sync(self):
        """Test synchronous notify method."""
        from haniel.config import WebhookConfig

        configs = [
            WebhookConfig(url="https://hooks.slack.com/xxx", format="slack"),
        ]
        notifier = WebhookNotifier(configs)

        msg = WebhookMessage(
            event_type=EventType.SERVICE_STARTED,
            service_name="test",
            message="Test message",
        )

        with patch("haniel.webhook.aiohttp.ClientSession") as mock_session_class:
            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)

            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.__aenter__ = AsyncMock(return_value=mock_response)
            mock_response.__aexit__ = AsyncMock(return_value=None)

            mock_session.post = MagicMock(return_value=mock_response)
            mock_session_class.return_value = mock_session

            # Use sync method
            notifier.notify_sync(msg)


class TestHelperMethods:
    """Tests for helper methods."""

    def test_service_started_helper(self):
        """Test helper for service started event."""
        from haniel.webhook import create_service_started_message

        msg = create_service_started_message("my-service")
        assert msg.event_type == EventType.SERVICE_STARTED
        assert msg.service_name == "my-service"

    def test_change_detected_helper(self):
        """Test helper for change detected event."""
        from haniel.webhook import create_change_detected_message

        commits = ["abc123: Fix bug", "def456: Add feature"]
        msg = create_change_detected_message("my-service", "my-repo", commits)
        assert msg.event_type == EventType.CHANGE_DETECTED
        assert msg.details["repo"] == "my-repo"
        assert msg.details["commits"] == commits

    def test_deploying_helper(self):
        """Test helper for deploying event."""
        from haniel.webhook import create_deploying_message

        msg = create_deploying_message("my-service")
        assert msg.event_type == EventType.DEPLOYING

    def test_deploy_complete_helper(self):
        """Test helper for deploy complete event."""
        from haniel.webhook import create_deploy_complete_message

        msg = create_deploy_complete_message("my-service")
        assert msg.event_type == EventType.DEPLOY_COMPLETE

    def test_graceful_failed_helper(self):
        """Test helper for graceful failed event."""
        from haniel.webhook import create_graceful_failed_message

        msg = create_graceful_failed_message("my-service", timeout=15)
        assert msg.event_type == EventType.GRACEFUL_FAILED
        assert msg.details["timeout"] == 15

    def test_circuit_breaker_helper(self):
        """Test helper for circuit breaker event."""
        from haniel.webhook import create_circuit_breaker_message

        msg = create_circuit_breaker_message(
            "my-service", failure_count=5, window=300
        )
        assert msg.event_type == EventType.CIRCUIT_BREAKER
        assert msg.details["failure_count"] == 5
        assert msg.details["window"] == 300
