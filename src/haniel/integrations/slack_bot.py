"""
Integrated Slack bot for haniel.

Sends DM notifications and handles interactive approve button via Slack Socket Mode.

The bot sends a DM to notify_user when:
  - Remote changes are detected (pending approval) — with approve button
  - A pull is in progress
  - A pull succeeds or fails

Socket Mode is used so no public URL is required.
The approve button triggers trigger_pull() via approve_callback.
"""

import logging
import threading
from typing import Any, Callable

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
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

        # Slack Bolt App + Socket Mode handler
        self._app = App(token=config.bot_token)
        self._handler = SocketModeHandler(self._app, config.app_token)
        self._socket_thread: threading.Thread | None = None

        # WebClient for direct API calls (chat.postMessage, etc.)
        self._client = WebClient(token=config.bot_token)
        self._dm_channel: str | None = None
        # Maps repo_name -> ts of the last "pending changes" DM (deleted on update)
        self._pending_ts: dict[str, str] = {}
        # Maps repo_name -> ts of the "배포 시작" DM (deleted when done)
        self._pulling_ts: dict[str, str] = {}
        self._lock = threading.Lock()

        # Register action handler for the approve button
        self._register_action_handlers()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Initialize the bot (open DM channel, start Socket Mode in background thread)."""
        try:
            self._dm_channel = self._open_dm_channel()
            logger.info("SlackBot DM channel: %s", self._dm_channel)
        except SlackApiError as e:
            logger.error("SlackBot failed to open DM channel: %s", e)
            # Continue — notifications are nice-to-have; don't abort runner startup

        # SocketModeHandler.start() is blocking, so run in a daemon thread
        self._socket_thread = threading.Thread(
            target=self._handler.start,
            daemon=True,
            name="slack-socket-mode",
        )
        self._socket_thread.start()
        logger.info("SlackBot Socket Mode started")
        self.notify_startup()

    def stop(self) -> None:
        """Shut down the bot (close Socket Mode connection)."""
        try:
            self._handler.close()
        except Exception as e:
            logger.warning("SlackBot stop error: %s", e)

    def _register_action_handlers(self) -> None:
        """Register Slack Bolt action handlers."""

        @self._app.action("approve_update")
        def handle_approve(ack, body, action):
            """Handle approve button click.

            Must ack() within 3 seconds, then spawn a separate thread for
            trigger_pull (which can take minutes) to avoid blocking the Socket
            Mode thread.
            """
            ack()
            repo_name = action.get("value", "")
            if not repo_name:
                logger.warning("approve_update action missing repo value")
                return
            if not self._approve_callback:
                logger.warning("approve_update received but no approve_callback set")
                return
            threading.Thread(
                target=self._approve_callback,
                args=(repo_name,),
                daemon=True,
                name=f"approve-{repo_name}",
            ).start()

    # ── Lifecycle Notifications ────────────────────────────────────────────────

    def notify_startup(self) -> None:
        """Send a startup notification to the DM channel (best-effort)."""
        if not self._dm_channel:
            return
        try:
            self._client.chat_postMessage(
                channel=self._dm_channel,
                text="✅ Haniel이 시작됐습니다.",
            )
        except Exception as e:
            # best-effort: startup notification failure must not abort runner startup
            logger.warning("Failed to send startup notification: %s", e)

    def notify_shutdown(self) -> None:
        """Send a shutdown notification to the DM channel (best-effort)."""
        if not self._dm_channel:
            return
        try:
            self._client.chat_postMessage(
                channel=self._dm_channel,
                text="🔴 Haniel이 종료됩니다.",
            )
        except Exception as e:
            # best-effort: shutdown notification failure must not abort runner shutdown
            logger.warning("Failed to send shutdown notification: %s", e)

    # ── Notifications ─────────────────────────────────────────────────────────

    def notify_pending(self, repo_name: str, pending_changes: dict) -> None:
        """Send (or replace) a DM showing pending changes with an approve button.

        If a previous pending message exists for this repo, it is deleted first
        so the user always sees the freshest diff.
        """
        if not self._dm_channel:
            return

        # Pop previous ts atomically (read + delete in one lock acquisition)
        with self._lock:
            old_ts = self._pending_ts.pop(repo_name, None)

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
                self._pulling_ts[repo_name] = ts

    def notify_done(
        self,
        repo_name: str,
        success: bool,
        pending_changes: dict | None = None,
        error: str | None = None,
    ) -> None:
        """Update the pulling DM to show success or failure."""
        if not self._dm_channel:
            return

        with self._lock:
            pulling_ts = self._pulling_ts.pop(repo_name, None)

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
            if pending_changes:
                commits: list[str] = pending_changes.get("commits", [])
                if commits:
                    shown = commits[:10]
                    commit_text = "\n".join(f"• {c}" for c in shown)
                    if len(commits) > 10:
                        commit_text += f"\n_...외 {len(commits) - 10}개_"
                    commit_section_text = self._truncate_for_block(
                        f"*배포된 커밋:*\n{commit_text}"
                    )
                    blocks.append(
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": commit_section_text},
                        }
                    )
                stat: str = pending_changes.get("stat", "")
                if stat:
                    stat_text = self._truncate_stat_for_block(
                        stat, prefix="*변경 통계:*\n```", suffix="```"
                    )
                    blocks.append(
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": stat_text},
                        }
                    )
            text = f"[{repo_name}] 배포 완료"
        else:
            error_text = self._truncate_for_block(
                f":x: *[{repo_name}] 배포 실패*\n```{error}```"
            )
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": error_text,
                    },
                }
            ]
            text = f"[{repo_name}] 배포 실패"

        self._post_blocks(blocks, text=text)

    # ── Block Kit helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _truncate_for_block(text: str, max_chars: int = 3000) -> str:
        """Truncate text to fit within max_chars, appending ellipsis if needed."""
        ellipsis = "\n...(생략)"
        if len(text) > max_chars:
            text = text[: max_chars - len(ellipsis)] + ellipsis
        return text

    @staticmethod
    def _truncate_stat_for_block(
        stat: str, prefix: str, suffix: str, max_chars: int = 3000
    ) -> str:
        """Wrap stat in prefix/suffix, truncating if the total exceeds max_chars."""
        ellipsis = "\n...(생략)"
        available = max_chars - len(prefix) - len(suffix)
        if len(stat) > available:
            stat = stat[: available - len(ellipsis)] + ellipsis
        return prefix + stat + suffix

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

        commit_section_text = self._truncate_for_block(f"*커밋 목록:*\n{commit_text}")

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
                    "text": commit_section_text,
                },
            },
        ]

        if stat:
            stat_text = self._truncate_stat_for_block(
                stat, prefix="*변경 통계:*\n```", suffix="```"
            )
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": stat_text},
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
