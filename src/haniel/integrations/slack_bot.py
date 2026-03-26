"""
Integrated Slack bot for haniel.

Sends DM notifications and handles interactive approve button via Slack Socket Mode.
Optionally provides an App Home dashboard when an AppHomeController is injected.

The bot sends a DM to notify_user when:
  - Remote changes are detected (pending approval) — with approve button
  - A pull is in progress
  - A pull succeeds or fails

Socket Mode is used so no public URL is required.
The approve button triggers trigger_pull() via approve_callback.
"""

import asyncio
import logging
import re
import threading
from typing import Any, Callable, Protocol, TYPE_CHECKING, runtime_checkable

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from ..config.model import SlackBotConfig

if TYPE_CHECKING:
    from ..core.claude_session import ClaudeSessionManager
    from ..dashboard.chat_broadcast import ChatBroadcaster

logger = logging.getLogger(__name__)

# ── State icon mapping ───────────────────────────────────────────────────────

STATE_ICONS: dict[str, str] = {
    "running": "🟢",
    "ready": "🟢",
    "starting": "🟠",
    "stopping": "🟠",
    "crashed": "🔴",
    "circuit_open": "🔴",
    "stopped": "⚫",
}


# ── AppHomeController Protocol ───────────────────────────────────────────────

@runtime_checkable
class AppHomeController(Protocol):
    """Duck-typed interface for the runner, used by App Home dashboard.

    The runner implements these methods directly; no import of this protocol
    is needed on the runner side.
    """

    def get_status(self) -> dict: ...
    def restart_service(self, name: str) -> str: ...
    def start_service(self, name: str) -> None: ...
    def stop_service(self, name: str) -> None: ...
    def enable_service(self, name: str) -> str: ...
    def trigger_pull(self, repo: str) -> None: ...
    def approve_self_update(self) -> str: ...
    def request_restart(self) -> str: ...


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
        app_home_controller: AppHomeController | None = None,
    ):
        self._config = config
        self._approve_callback = approve_callback
        self._app_home_controller = app_home_controller

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

        # Session-level locks for sequential DM handling (keyed by session_id)
        self._session_locks: dict[str, asyncio.Lock] = {}

        # Register action handler for the approve button
        self._register_action_handlers()

        # Register App Home handlers if controller is available
        if self._app_home_controller is not None:
            self._register_app_home_handlers()

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

    # ── Chat Session Methods ────────────────────────────────────────────────

    def create_chat_thread(self, session_id: str, user_id: str) -> str | None:
        """Post an initial DM to start a chat thread. Returns thread_ts or None."""
        try:
            resp = self._client.chat_postMessage(
                channel=user_id,
                text="💬 새 채팅 세션이 시작됐습니다.",
            )
            return resp["ts"]
        except Exception as e:
            logger.warning("create_chat_thread failed: %s", e)
            return None

    def post_chat_message(self, user_id: str, thread_ts: str, text: str) -> None:
        """Post a message to a chat DM thread (best-effort)."""
        try:
            self._client.chat_postMessage(
                channel=user_id,
                thread_ts=thread_ts,
                text=text,
            )
        except Exception as e:
            logger.warning("post_chat_message failed: %s", e)

    def post_compaction_start(self, user_id: str, thread_ts: str) -> str | None:
        """Post a compaction-in-progress notice. Returns message ts or None."""
        try:
            resp = self._client.chat_postMessage(
                channel=user_id,
                thread_ts=thread_ts,
                text="🔄 컴팩션 진행 중...",
            )
            return resp["ts"]
        except Exception as e:
            logger.warning("post_compaction_start failed: %s", e)
            return None

    def update_compaction_done(
        self, user_id: str, thread_ts: str, msg_ts: str
    ) -> None:
        """Replace the compaction notice with a completion message."""
        try:
            self._client.chat_update(
                channel=user_id,
                ts=msg_ts,
                text="✅ 컴팩션 완료",
            )
        except Exception as e:
            logger.warning("update_compaction_done failed: %s", e)

    def post_error(self, user_id: str, thread_ts: str, error: str) -> None:
        """Post an error message to the chat DM thread (best-effort)."""
        try:
            self._client.chat_postMessage(
                channel=user_id,
                thread_ts=thread_ts,
                text=f"❌ 오류: {error}",
            )
        except Exception as e:
            logger.warning("post_error failed: %s", e)

    # ── DM Chat Handler ────────────────────────────────────────────────────

    def _register_dm_handler(
        self,
        loop: "asyncio.AbstractEventLoop",
        session_manager: "ClaudeSessionManager",
        broadcaster: "ChatBroadcaster",
    ) -> None:
        """Register Bolt message event handler for DM events.

        Bridges synchronous Bolt Socket Mode into the async event loop via
        run_coroutine_threadsafe. Called once in DashboardWebSocket.setup(loop).
        """

        @self._app.event("message")
        def handle_dm_message(event, say):
            # Only handle DMs, ignore bot's own messages
            if event.get("channel_type") != "im":
                return
            if event.get("bot_id"):
                return
            future = asyncio.run_coroutine_threadsafe(
                self._handle_dm_async(session_manager, broadcaster, event),
                loop,
            )
            try:
                future.result(timeout=120)
            except Exception as e:
                logger.error("DM 처리 실패: %s", e)

    async def _handle_dm_async(
        self,
        session_manager: "ClaudeSessionManager",
        broadcaster: "ChatBroadcaster",
        event: dict,
    ) -> None:
        """Handle an incoming DM event: find/create session, stream, relay.

        New DM (no thread_ts): creates new session.
        Thread reply (thread_ts set): resumes bound session.
        """
        text = (event.get("text") or "").strip()
        if not text:
            return

        channel_id = event.get("channel", "")
        # Top-level DM has no thread_ts; its own ts becomes the thread anchor.
        thread_ts = event.get("thread_ts") or event.get("ts", "")

        if not thread_ts or not channel_id:
            logger.warning("DM event missing thread_ts or channel: %s", event)
            return

        # Find or create session bound to this thread
        session = session_manager.get_session_by_thread_ts(thread_ts)
        if session is None:
            session_id = session_manager.create_session()
            session_manager.update_slack_binding(session_id, thread_ts, channel_id)
        else:
            session_id = session["id"]

        # Per-session lock prevents concurrent input processing
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            compaction_msg_ts: str | None = None
            buffer: list[str] = []

            async for evt in session_manager.stream_message(session_id, text):
                evt_type = evt.get("type")

                if evt_type == "text_delta":
                    buffer.append(evt.get("delta", ""))

                elif evt_type == "message_end":
                    full_text = "".join(buffer)
                    if full_text:
                        self.post_chat_message(channel_id, thread_ts, full_text)
                    buffer.clear()

                elif evt_type == "compact_start":
                    compaction_msg_ts = self.post_compaction_start(
                        channel_id, thread_ts
                    )

                elif evt_type == "compact_end":
                    if compaction_msg_ts:
                        self.update_compaction_done(
                            channel_id, thread_ts, compaction_msg_ts
                        )
                        compaction_msg_ts = None

                elif evt_type == "error":
                    self.post_error(channel_id, thread_ts, evt.get("error", ""))

                # Broadcast all events to watching dashboard WS clients
                await broadcaster.broadcast(session_id, evt)

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

    def notify_crash(self, service_name: str) -> None:
        """Send a crash notification to the DM channel (best-effort)."""
        if not self._dm_channel:
            return
        try:
            self._client.chat_postMessage(
                channel=self._dm_channel,
                text=f"🔴 서비스 '{service_name}'이 크래시되었습니다.",
            )
        except Exception as e:
            logger.warning("Failed to send crash notification: %s", e)

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
        discarded_changes: list[str] | None = None,
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
            if discarded_changes:
                lines = "\n".join(f"• `{f}`" for f in discarded_changes)
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"⚠️ *로컬 변경사항 드롭됨 (force pull)*\n{lines}",
                        },
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

    # ── App Home dashboard ───────────────────────────────────────────────────

    def _register_app_home_handlers(self) -> None:
        """Register event and action handlers for the App Home dashboard."""
        controller = self._app_home_controller

        @self._app.event("app_home_opened")
        def handle_app_home_opened(event, client, logger):
            user_id = event["user"]
            try:
                status = controller.get_status()
                view = self._build_home_view(status)
            except Exception as e:
                logger.error("Failed to build App Home view: %s", e)
                view = self._build_error_view(str(e))
            client.views_publish(user_id=user_id, view=view)

        @self._app.action(re.compile(r"^svc_menu_"))
        def handle_svc_menu(ack, body, client, logger):
            ack()
            action = body["actions"][0]
            selected = action["selected_option"]["value"]
            command, target = selected.split(":", 1)
            user_id = body["user"]["id"]
            try:
                if command == "restart":
                    controller.restart_service(target)
                elif command == "start":
                    controller.start_service(target)
                elif command == "stop":
                    controller.stop_service(target)
                elif command == "enable":
                    controller.enable_service(target)
                # Refresh the App Home view after successful action
                self._refresh_home_view(controller, client, user_id, logger)
            except Exception as e:
                logger.error("svc_menu action failed: %s", e)
                client.chat_postEphemeral(
                    channel=user_id,
                    user=user_id,
                    text=f"❌ 작업 실패: {e}",
                )

        @self._app.action(re.compile(r"^update_repo_"))
        def handle_update_repo(ack, body, client, logger):
            ack()
            action = body["actions"][0]
            value = action["value"]
            command, target = value.split(":", 1)
            user_id = body["user"]["id"]
            try:
                if command == "update":
                    controller.approve_self_update()
                else:
                    threading.Thread(
                        target=controller.trigger_pull,
                        args=(target,),
                        daemon=True,
                        name=f"app-home-pull-{target}",
                    ).start()
                # Refresh the App Home view after action
                self._refresh_home_view(controller, client, user_id, logger)
            except Exception as e:
                logger.error("update_repo action failed: %s", e)
                client.chat_postEphemeral(
                    channel=user_id,
                    user=user_id,
                    text=f"❌ 업데이트 실패: {e}",
                )

    def _refresh_home_view(self, controller, client, user_id: str, logger) -> None:
        """Refresh the App Home view after an action (best-effort)."""
        try:
            status = controller.get_status()
            view = self._build_home_view(status)
            client.views_publish(user_id=user_id, view=view)
        except Exception as e:
            logger.warning("Failed to refresh App Home view: %s", e)

    def _build_home_view(self, status: dict) -> dict[str, Any]:
        """Build the App Home view from runner status."""
        blocks: list[dict[str, Any]] = []
        blocks += self._build_header_blocks(status)
        blocks += self._build_haniel_block(status)
        blocks += self._build_service_blocks(status)
        blocks += self._build_update_blocks(status)
        return {"type": "home", "blocks": blocks}

    def _build_error_view(self, error: str) -> dict[str, Any]:
        """Build an error App Home view."""
        return {
            "type": "home",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "⚠️ 하니엘 대시보드",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"❌ 상태를 불러오지 못했습니다.\n```{error}```",
                    },
                },
            ],
        }

    def _build_header_blocks(self, status: dict) -> list[dict[str, Any]]:
        """Build the header section of the App Home view."""
        return [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🏠 하니엘 서비스 대시보드",
                    "emoji": True,
                },
            },
        ]

    def _build_haniel_block(self, status: dict) -> list[dict[str, Any]]:
        """Build the Haniel service row (always first)."""
        start_time = status.get("start_time", "")
        icon = "🟢" if status.get("running") else "⚫"
        text = f"{icon}  *haniel*"
        if start_time:
            text += f"  |  시작: `{start_time[:19]}`"

        block: dict[str, Any] = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        }

        options = self._build_overflow_options("haniel", "running" if status.get("running") else "stopped")
        if options:
            block["accessory"] = {
                "type": "overflow",
                "action_id": "svc_menu_haniel",
                "options": options,
            }

        return [block]

    def _build_service_blocks(self, status: dict) -> list[dict[str, Any]]:
        """Build service rows for all managed services."""
        blocks: list[dict[str, Any]] = []
        services = status.get("services", {})

        for name, info in sorted(services.items()):
            state = info.get("state", "stopped")
            icon = STATE_ICONS.get(state, "⚫")
            config = info.get("config", {})
            ready = config.get("ready", "")

            text = f"{icon}  *{name}*"
            if ready:
                text += f"  |  `{ready}`"
            uptime = info.get("uptime", 0)
            if uptime and state in ("running", "ready"):
                hours = int(uptime // 3600)
                minutes = int((uptime % 3600) // 60)
                text += f"  |  {hours}h {minutes}m"

            block: dict[str, Any] = {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            }

            options = self._build_overflow_options(name, state)
            if options:
                block["accessory"] = {
                    "type": "overflow",
                    "action_id": f"svc_menu_{name}",
                    "options": options,
                }

            blocks.append(block)

        return blocks

    def _build_overflow_options(
        self, name: str, state: str
    ) -> list[dict[str, Any]]:
        """Build overflow menu options based on service state."""
        options: list[dict[str, Any]] = []

        if state in ("running", "ready", "starting", "stopping"):
            options.append({
                "text": {"type": "plain_text", "text": "🔄 재시작"},
                "value": f"restart:{name}",
            })
            if name != "haniel":
                options.append({
                    "text": {"type": "plain_text", "text": "⏹️ 중지"},
                    "value": f"stop:{name}",
                })

        if state in ("stopped", "crashed", "circuit_open"):
            options.append({
                "text": {"type": "plain_text", "text": "▶️ 시작"},
                "value": f"start:{name}",
            })

        if state == "circuit_open":
            options.append({
                "text": {"type": "plain_text", "text": "🔓 서킷 리셋"},
                "value": f"enable:{name}",
            })

        return options

    def _build_update_blocks(self, status: dict) -> list[dict[str, Any]]:
        """Build update section showing repos with pending changes."""
        blocks: list[dict[str, Any]] = []
        repos = status.get("repos", {})
        self_update = status.get("self_update")

        # Collect repos with pending changes
        update_items: list[tuple[str, dict, bool]] = []  # (name, repo_info, is_self)

        for repo_name, repo_info in repos.items():
            pending = repo_info.get("pending_changes")
            if not pending:
                continue
            is_self = (
                self_update is not None
                and self_update.get("repo") == repo_name
                and self_update.get("pending")
            )
            update_items.append((repo_name, repo_info, is_self))

        if not update_items:
            return blocks

        blocks.append({
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "📦 업데이트 대기",
                "emoji": True,
            },
        })

        for repo_name, repo_info, is_self in update_items:
            pending = repo_info["pending_changes"]
            commits = pending.get("commits", [])
            commit_summary = commits[0] if commits else ""
            count = len(commits)
            text = f"*{repo_name}*"
            if count:
                text += f"  |  {count}개 커밋"
            if commit_summary:
                text += f"\n`{commit_summary[:60]}`"

            if is_self:
                action_id = f"update_repo_{repo_name}"
                value = f"update:{repo_name}"
            else:
                action_id = f"update_repo_{repo_name}"
                value = f"pull:{repo_name}"

            block: dict[str, Any] = {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
                "accessory": {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "🚀 업데이트" if is_self else "📥 배포",
                        "emoji": True,
                    },
                    "action_id": action_id,
                    "value": value,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "확인"},
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{repo_name}*을(를) {'업데이트' if is_self else '배포'}하시겠습니까?",
                        },
                        "confirm": {"type": "plain_text", "text": "실행"},
                        "deny": {"type": "plain_text", "text": "취소"},
                    },
                },
            }
            blocks.append(block)

        return blocks
