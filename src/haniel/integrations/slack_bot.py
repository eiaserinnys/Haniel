"""
Integrated Slack bot for haniel.

Phase 1: Sends DM notifications via WebClient (no interactive buttons yet).
Phase 2: Adds Socket Mode + approve button interaction.

The bot sends a DM to notify_user when:
  - Remote changes are detected (pending approval)
  - A pull is in progress
  - A pull succeeds or fails
"""

import logging
import threading
from typing import Any, Callable

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from ..config.model import SlackBotConfig

logger = logging.getLogger(__name__)


class SlackBot:
    """Manages Slack DM notifications for repository changes.

    Lifecycle:
    1. Created with SlackBotConfig (and optional approve_callback for Phase 2)
    2. start() opens the DM channel (and starts Socket Mode in Phase 2)
    3. notify_pending / notify_pulling / notify_done send/update DMs
    4. stop() shuts down Socket Mode (Phase 2)

    Thread safety: all public methods are called from the runner's poll thread
    or from a trigger_pull thread, never from async context.
    """

    def __init__(
        self,
        config: SlackBotConfig,
        approve_callback: Callable[[str], None] | None = None,
    ):
        self._config = config
        self._approve_callback = approve_callback
        self._client = WebClient(token=config.bot_token)
        self._dm_channel: str | None = None
        # Maps repo_name -> ts of the last "pending" message (used to delete on update)
        self._pending_ts: dict[str, str] = {}
        self._lock = threading.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Initialize the bot (open DM channel, start Socket Mode in Phase 2)."""
        try:
            self._dm_channel = self._open_dm_channel()
            logger.info("SlackBot started, DM channel: %s", self._dm_channel)
        except SlackApiError as e:
            logger.error("SlackBot failed to start: %s", e)

    def stop(self) -> None:
        """Shut down the bot."""
        pass  # Phase 2: stop SocketModeHandler here

    # ── Notifications ─────────────────────────────────────────────────────────

    def notify_pending(self, repo_name: str, pending_changes: dict) -> None:
        """Send (or replace) a DM showing pending changes with an approve button.

        If a previous pending message exists for this repo, it is deleted first
        so the user always sees the freshest diff.
        """
        if not self._dm_channel:
            return

        # Delete any previous pending message for this repo
        with self._lock:
            old_ts = self._pending_ts.get(repo_name)

        if old_ts:
            self._delete_message(old_ts)

        blocks = self._build_pending_blocks(repo_name, pending_changes)
        ts = self._post_blocks(blocks, text=f"[{repo_name}] 업데이트 대기 중")
        if ts:
            with self._lock:
                self._pending_ts[repo_name] = ts

    def notify_pulling(self, repo_name: str, auto: bool = False) -> None:
        """Update the pending DM to show that a pull is in progress.

        If triggered automatically (auto=True), also sends an informational message.
        If triggered manually, replaces the pending message.
        """
        if not self._dm_channel:
            return

        with self._lock:
            old_ts = self._pending_ts.pop(repo_name, None)

        if old_ts:
            self._delete_message(old_ts)

        if auto:
            label = "자동 배포 시작"
        else:
            label = "배포 시작"

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":arrows_counterclockwise: *[{repo_name}] {label}*\ngit pull 중입니다...",
                },
            }
        ]
        ts = self._post_blocks(blocks, text=f"[{repo_name}] {label}")
        if ts:
            with self._lock:
                # Store as pulling_ts so notify_done can update it
                self._pending_ts[f"_pulling_{repo_name}"] = ts

    def notify_done(
        self, repo_name: str, success: bool, error: str | None = None
    ) -> None:
        """Update the pulling DM to show success or failure."""
        if not self._dm_channel:
            return

        with self._lock:
            pulling_ts = self._pending_ts.pop(f"_pulling_{repo_name}", None)

        if pulling_ts:
            self._delete_message(pulling_ts)

        if success:
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":white_check_mark: *[{repo_name}] 배포 완료*",
                    },
                }
            ]
            text = f"[{repo_name}] 배포 완료"
        else:
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":x: *[{repo_name}] 배포 실패*\n```{error}```",
                    },
                }
            ]
            text = f"[{repo_name}] 배포 실패"

        self._post_blocks(blocks, text=text)

    # ── Block Kit helpers ──────────────────────────────────────────────────────

    def _build_pending_blocks(
        self, repo_name: str, pending_changes: dict
    ) -> list[dict[str, Any]]:
        """Build Block Kit message for pending changes (with approve button)."""
        commits: list[str] = pending_changes.get("commits", [])
        stat: str = pending_changes.get("stat", "")

        commit_text = ""
        if commits:
            shown = commits[:10]
            commit_text = "\n".join(f"• {c}" for c in shown)
            if len(commits) > 10:
                commit_text += f"\n_...외 {len(commits) - 10}개_"
        else:
            commit_text = "_커밋 정보 없음_"

        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f":eyes: [{repo_name}] 업데이트 대기 중",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*커밋 목록:*\n{commit_text}",
                },
            },
        ]

        if stat:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*변경 통계:*\n```{stat}```"},
                }
            )

        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": ":rocket: 배포 승인",
                            "emoji": True,
                        },
                        "style": "primary",
                        "action_id": "approve_update",
                        "value": repo_name,
                    }
                ],
            }
        )

        return blocks

    # ── Slack API wrappers ─────────────────────────────────────────────────────

    def _open_dm_channel(self) -> str:
        """Open a DM channel with notify_user and return the channel ID."""
        response = self._client.conversations_open(users=self._config.notify_user)
        return response["channel"]["id"]

    def _post_blocks(
        self, blocks: list[dict], text: str = ""
    ) -> str | None:
        """Post a Block Kit message and return the message ts."""
        if not self._dm_channel:
            return None
        try:
            response = self._client.chat_postMessage(
                channel=self._dm_channel,
                blocks=blocks,
                text=text,
            )
            return response["ts"]
        except SlackApiError as e:
            logger.warning("SlackBot post failed: %s", e)
            return None

    def _delete_message(self, ts: str) -> None:
        """Delete a message by ts."""
        if not self._dm_channel:
            return
        try:
            self._client.chat_delete(channel=self._dm_channel, ts=ts)
        except SlackApiError as e:
            logger.warning("SlackBot delete failed: %s", e)
