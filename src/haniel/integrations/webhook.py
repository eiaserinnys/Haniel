"""
Webhook notification module for haniel.

Supports Slack (Block Kit), Discord, and generic JSON webhooks.
haniel sends notifications for service lifecycle events.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import httpx

from ..config import WebhookConfig

logger = logging.getLogger(__name__)


class WebhookFormat(str, Enum):
    """Supported webhook formats."""

    SLACK = "slack"
    DISCORD = "discord"
    JSON = "json"


class EventType(str, Enum):
    """Types of events that can trigger webhooks."""

    SERVICE_STARTED = "service_started"
    CHANGE_DETECTED = "change_detected"
    DEPLOYING = "deploying"
    DEPLOY_COMPLETE = "deploy_complete"
    GRACEFUL_FAILED = "graceful_failed"
    CIRCUIT_BREAKER = "circuit_breaker"
    SELF_UPDATE_DETECTED = "self_update_detected"
    SELF_UPDATE_APPROVED = "self_update_approved"


# Event metadata for styling
EVENT_METADATA = {
    EventType.SERVICE_STARTED: {
        "emoji": ":rocket:",
        "color": 0x2ECC71,  # Green
        "slack_color": "good",
        "title": "Service Started",
    },
    EventType.CHANGE_DETECTED: {
        "emoji": ":eyes:",
        "color": 0x3498DB,  # Blue
        "slack_color": "#3498DB",
        "title": "Changes Detected",
    },
    EventType.DEPLOYING: {
        "emoji": ":arrows_counterclockwise:",
        "color": 0xF39C12,  # Orange
        "slack_color": "warning",
        "title": "Deploying",
    },
    EventType.DEPLOY_COMPLETE: {
        "emoji": ":white_check_mark:",
        "color": 0x2ECC71,  # Green
        "slack_color": "good",
        "title": "Deploy Complete",
    },
    EventType.GRACEFUL_FAILED: {
        "emoji": ":warning:",
        "color": 0xE74C3C,  # Red
        "slack_color": "danger",
        "title": "Graceful Shutdown Failed",
    },
    EventType.CIRCUIT_BREAKER: {
        "emoji": ":rotating_light:",
        "color": 0xE74C3C,  # Red
        "slack_color": "danger",
        "title": "Circuit Breaker Tripped",
    },
    EventType.SELF_UPDATE_DETECTED: {
        "emoji": ":package:",
        "color": 0x9B59B6,  # Purple
        "slack_color": "#9B59B6",
        "title": "Self-Update Available",
    },
    EventType.SELF_UPDATE_APPROVED: {
        "emoji": ":white_check_mark:",
        "color": 0x2ECC71,  # Green
        "slack_color": "good",
        "title": "Self-Update Approved",
    },
}


@dataclass
class WebhookMessage:
    """A webhook notification message."""

    event_type: EventType
    service_name: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def format_slack_message(msg: WebhookMessage) -> dict[str, Any]:
    """Format a message for Slack using Block Kit.

    Args:
        msg: The webhook message

    Returns:
        Slack Block Kit payload
    """
    metadata = EVENT_METADATA[msg.event_type]

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{metadata['emoji']} {metadata['title']}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Service:* `{msg.service_name}`\n{msg.message}",
            },
        },
    ]

    # Add details if present
    if msg.details:
        fields = []
        for key, value in msg.details.items():
            if isinstance(value, list):
                value_str = "\n".join(f"• {v}" for v in value[:5])
                if len(value) > 5:
                    value_str += f"\n_...and {len(value) - 5} more_"
            else:
                value_str = str(value)

            fields.append({"type": "mrkdwn", "text": f"*{key}:*\n{value_str}"})

        # Split fields into groups of 2 (Slack limit)
        for i in range(0, len(fields), 2):
            blocks.append({"type": "section", "fields": fields[i : i + 2]})

    # Add timestamp footer
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"⏱️ {msg.timestamp.strftime('%Y-%m-%d %H:%M:%S')}",
                }
            ],
        }
    )

    return {
        "blocks": blocks,
        "attachments": [{"color": metadata["slack_color"], "blocks": []}],
    }


def format_discord_message(msg: WebhookMessage) -> dict[str, Any]:
    """Format a message for Discord webhook.

    Args:
        msg: The webhook message

    Returns:
        Discord webhook payload
    """
    metadata = EVENT_METADATA[msg.event_type]

    embed = {
        "title": f"{metadata['title']}",
        "description": f"**Service:** `{msg.service_name}`\n\n{msg.message}",
        "color": metadata["color"],
        "timestamp": msg.timestamp.isoformat(),
    }

    # Add fields for details
    if msg.details:
        fields = []
        for key, value in msg.details.items():
            if isinstance(value, list):
                value_str = "\n".join(f"• {v}" for v in value[:5])
                if len(value) > 5:
                    value_str += f"\n_...and {len(value) - 5} more_"
            else:
                value_str = str(value)

            fields.append({"name": key, "value": value_str, "inline": True})

        embed["fields"] = fields

    return {"embeds": [embed]}


def format_json_message(msg: WebhookMessage) -> dict[str, Any]:
    """Format a message as generic JSON.

    Args:
        msg: The webhook message

    Returns:
        Generic JSON payload
    """
    return {
        "event": msg.event_type.value,
        "service": msg.service_name,
        "message": msg.message,
        "details": msg.details,
        "timestamp": msg.timestamp.isoformat(),
    }


class WebhookNotifier:
    """Sends notifications to configured webhooks."""

    def __init__(self, configs: list[WebhookConfig]):
        """Initialize the notifier with webhook configurations.

        Args:
            configs: List of webhook configurations
        """
        self.webhooks = [
            {"url": cfg.url, "format": WebhookFormat(cfg.format)} for cfg in configs
        ]

    async def notify(self, msg: WebhookMessage) -> None:
        """Send a notification to all configured webhooks.

        Args:
            msg: The message to send
        """
        if not self.webhooks:
            return

        async with httpx.AsyncClient(timeout=10.0) as client:
            tasks = [
                self._send_webhook(client, webhook, msg) for webhook in self.webhooks
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_webhook(
        self,
        client: httpx.AsyncClient,
        webhook: dict[str, Any],
        msg: WebhookMessage,
    ) -> None:
        """Send a message to a single webhook.

        Args:
            client: The httpx async client
            webhook: Webhook configuration (url, format)
            msg: The message to send
        """
        try:
            # Format the message based on webhook format
            fmt = webhook["format"]
            if fmt == WebhookFormat.SLACK:
                payload = format_slack_message(msg)
            elif fmt == WebhookFormat.DISCORD:
                payload = format_discord_message(msg)
            else:
                payload = format_json_message(msg)

            response = await client.post(webhook["url"], json=payload)
            if response.status_code >= 400:
                logger.warning("Webhook returned status %s", response.status_code)
        except httpx.TimeoutException:
            logger.warning("Webhook timed out")
        except Exception:
            logger.warning("Webhook failed")

    def notify_sync(self, msg: WebhookMessage) -> None:
        """Synchronous wrapper for notify().

        Args:
            msg: The message to send
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            # If there's already an event loop, create a task
            asyncio.create_task(self.notify(msg))
        else:
            # Otherwise, create a new event loop
            asyncio.run(self.notify(msg))


# Helper functions to create common messages
