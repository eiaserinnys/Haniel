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
from urllib.parse import urlparse

import aiohttp

from ..config import WebhookConfig

logger = logging.getLogger(__name__)


def _mask_url(url: str) -> str:
    """Mask sensitive parts of webhook URL for logging.

    Args:
        url: The webhook URL to mask

    Returns:
        Masked URL safe for logging
    """
    try:
        parsed = urlparse(url)
        path = parsed.path
        if len(path) > 15:
            path = f"{path[:5]}...{path[-5:]}"
        return f"{parsed.scheme}://{parsed.netloc}{path}"
    except Exception:
        return "<invalid-url>"


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

        async with aiohttp.ClientSession() as session:
            tasks = [
                self._send_webhook(session, webhook, msg) for webhook in self.webhooks
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_webhook(
        self,
        session: aiohttp.ClientSession,
        webhook: dict[str, Any],
        msg: WebhookMessage,
    ) -> None:
        """Send a message to a single webhook.

        Args:
            session: The aiohttp session
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

            async with session.post(
                webhook["url"],
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status >= 400:
                    logger.warning(
                        f"Webhook returned status {response.status}: {_mask_url(webhook['url'])}"
                    )
        except asyncio.TimeoutError:
            logger.warning(f"Webhook timed out: {_mask_url(webhook['url'])}")
        except Exception as e:
            logger.warning(f"Webhook failed: {_mask_url(webhook['url'])} - {e}")

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


def create_service_started_message(service_name: str) -> WebhookMessage:
    """Create a service started message.

    Args:
        service_name: Name of the service

    Returns:
        WebhookMessage for service started event
    """
    return WebhookMessage(
        event_type=EventType.SERVICE_STARTED,
        service_name=service_name,
        message="Service has been started successfully.",
    )


def create_change_detected_message(
    service_name: str,
    repo: str,
    commits: list[str],
) -> WebhookMessage:
    """Create a change detected message.

    Args:
        service_name: Name of the service
        repo: Repository name
        commits: List of commit messages

    Returns:
        WebhookMessage for change detected event
    """
    return WebhookMessage(
        event_type=EventType.CHANGE_DETECTED,
        service_name=service_name,
        message=f"Changes detected in repository `{repo}`.",
        details={"repo": repo, "commits": commits},
    )


def create_deploying_message(service_name: str) -> WebhookMessage:
    """Create a deploying message.

    Args:
        service_name: Name of the service

    Returns:
        WebhookMessage for deploying event
    """
    return WebhookMessage(
        event_type=EventType.DEPLOYING,
        service_name=service_name,
        message="Pulling changes and restarting service...",
    )


def create_deploy_complete_message(service_name: str) -> WebhookMessage:
    """Create a deploy complete message.

    Args:
        service_name: Name of the service

    Returns:
        WebhookMessage for deploy complete event
    """
    return WebhookMessage(
        event_type=EventType.DEPLOY_COMPLETE,
        service_name=service_name,
        message="Deployment complete. Service is running.",
    )


def create_graceful_failed_message(
    service_name: str,
    timeout: int,
) -> WebhookMessage:
    """Create a graceful shutdown failed message.

    Args:
        service_name: Name of the service
        timeout: Timeout that was exceeded

    Returns:
        WebhookMessage for graceful failed event
    """
    return WebhookMessage(
        event_type=EventType.GRACEFUL_FAILED,
        service_name=service_name,
        message=f"Graceful shutdown timed out after {timeout}s. Force killing process.",
        details={"timeout": timeout},
    )


def create_circuit_breaker_message(
    service_name: str,
    failure_count: int,
    window: int,
) -> WebhookMessage:
    """Create a circuit breaker message.

    Args:
        service_name: Name of the service
        failure_count: Number of failures that triggered the circuit breaker
        window: Time window in seconds

    Returns:
        WebhookMessage for circuit breaker event
    """
    return WebhookMessage(
        event_type=EventType.CIRCUIT_BREAKER,
        service_name=service_name,
        message=f"Circuit breaker tripped: {failure_count} failures in {window}s. Service disabled.",
        details={"failure_count": failure_count, "window": window},
    )
