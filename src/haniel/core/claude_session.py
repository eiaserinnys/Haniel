"""
Claude Code session manager for haniel dashboard chat panel.

Manages session metadata persistence and SDK-based streaming
via claude-agent-sdk (ClaudeSDKClient).
"""

import copy
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, TYPE_CHECKING

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, HookContext
from claude_agent_sdk.types import (
    AssistantMessage,
    HookJSONOutput,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

MAX_COMPACT_RETRIES = 3
COMPACT_RETRY_READ_TIMEOUT = 30  # seconds

if TYPE_CHECKING:
    from .runner import ServiceRunner

logger = logging.getLogger(__name__)

# Metadata file stored in runner.config_dir
_SESSIONS_FILE = "chat_sessions.json"


class ClaudeSessionManager:
    """Manages Claude Code SDK sessions for the dashboard chat panel.

    Session metadata is persisted to disk at runner.config_dir/chat_sessions.json.
    Each session maps a haniel UUID to a Claude CLI session ID (used with resume).

    The SDK client is kept alive per session so that MCP connections are
    established only once, eliminating the ~5 s overhead of subprocess startup.

    Usage::

        manager = ClaudeSessionManager(runner)
        async for event in manager.stream_message(None, "hello"):
            # event: {"type": "text_delta", "delta": "..."}
            #        {"type": "session_start", "session_id": "<uuid>"}
            #        {"type": "message_end", "session_id": "<uuid>"}
            #        {"type": "error", "error": "..."}
            ...
    """

    def __init__(self, runner: "ServiceRunner"):
        self.runner = runner
        self._sessions_path = runner.config_dir / _SESSIONS_FILE
        self._data: dict = {"sessions": [], "last_session_id": None}
        self._clients: dict[str, ClaudeSDKClient] = {}

        # Workspace directory for Claude Code sessions.
        # This is the cwd passed to ClaudeAgentOptions. The SDK reads
        # .mcp.json from cwd *and walks up the directory tree*.
        # IMPORTANT: Must NOT be a descendant of workspace/ because
        # workspace/.mcp.json contains 11 MCP servers for soulstream sessions,
        # which would be loaded into the dashboard chat CLI and cause startup
        # failures (exit code 1).
        # Path: {root}/.self/workspace — the haniel repo's runtime workspace.
        # Source: .projects/haniel/workspace/ (rules, skills, .mcp.json)
        # Deployed: .self/workspace/ via git pull
        # .self/.gitignore lists workspace/ so it's runtime-only local data.
        self._workspace_path = runner.config_dir / ".self" / "workspace"
        self._workspace_path.mkdir(parents=True, exist_ok=True)

        # CLI stderr log directory (same as workspace for simplicity)
        self._stderr_log_dir = self._workspace_path / "logs"
        self._stderr_log_dir.mkdir(parents=True, exist_ok=True)

        self._load_sessions()
        self._write_mcp_config()

    # ── Public API ────────────────────────────────────────────────────────────

    def list_sessions(self) -> list[dict]:
        """Return a deep copy of all sessions (safe to iterate; won't mutate internal state)."""
        return copy.deepcopy(self._data["sessions"])

    def create_session(self) -> str:
        """Create and register a new session, returning its haniel UUID.

        Used by the chat panel's new_session command so the UUID returned
        to the client is immediately usable for subsequent send_message calls.
        """
        session = self._make_session()
        self._data["sessions"].append(session)
        self._data["last_session_id"] = session["id"]
        self._save_sessions()
        return session["id"]

    def get_last_session(self) -> dict | None:
        """Return the most recently active session, or None."""
        last_id = self._data.get("last_session_id")
        if not last_id:
            return None
        return self._find_session(last_id)

    def get_session(self, session_id: str) -> dict | None:
        """Return a session by haniel UUID, or None."""
        return self._find_session(session_id)

    async def stream_message(
        self,
        session_id: str | None,
        text: str,
    ) -> AsyncGenerator[dict, None]:
        """Stream a message to Claude and yield events.

        Args:
            session_id: haniel session UUID, or None to use a new session.
            text: User message text.

        Yields:
            ``{"type": "session_start", "session_id": "<haniel uuid>"}``
            ``{"type": "text_delta", "delta": "..."}``
            ``{"type": "message_end", "session_id": "<haniel uuid>"}``
            ``{"type": "error", "error": "..."}``
        """
        # Resolve session metadata
        session = None
        if session_id:
            session = self._find_session(session_id)

        is_new = session is None
        if is_new:
            session = self._make_session()
            session_id = session["id"]
            self._data["sessions"].append(session)

        self._data["last_session_id"] = session_id

        claude_session_id: str | None = session.get("claude_session_id")

        compact_events: list = []
        compact_retry_count = 0
        last_text_parts: list[str] = []
        new_claude_session_id: str | None = None
        client: ClaudeSDKClient | None = None
        resumed: bool = False
        stderr_files: list = []  # file handles to close in finally

        try:
            client, resumed, sf = await self._get_or_create_client(
                claude_session_id, compact_events=compact_events
            )
            if sf:
                stderr_files.append(sf)

            # Resume 실패로 새 세션이 생성된 경우, 기존 claude_session_id 초기화
            if claude_session_id and not resumed:
                session["claude_session_id"] = None
                claude_session_id = None
                self._save_sessions()

            yield {
                "type": "session_start",
                "session_id": session_id,
                "is_new": is_new,
                "resumed": resumed and not is_new,
            }

            await client.query(text)

            # Compact retry loop: if auto-compact happens during receive_response(),
            # the CLI may terminate after sending ResultMessage. We detect this via
            # the PreCompact hook and retry with a resumed client.
            while True:
                compact_before = len(compact_events)

                async for msg in client.receive_response():
                    if isinstance(msg, SystemMessage):
                        sid = msg.data.get("session_id") if hasattr(msg, "data") else None
                        if not new_claude_session_id and sid:
                            new_claude_session_id = sid

                    elif isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                last_text_parts.append(block.text)
                                yield {"type": "text_delta", "delta": block.text}

                    elif isinstance(msg, ResultMessage):
                        if msg.session_id:
                            new_claude_session_id = msg.session_id

                # Compact retry decision
                compact_happened = len(compact_events) > compact_before
                if compact_happened and compact_retry_count < MAX_COMPACT_RETRIES:
                    compact_retry_count += 1
                    yield {
                        "type": "compact_start",
                        "retry": compact_retry_count,
                        "max_retries": MAX_COMPACT_RETRIES,
                    }
                    logger.info(
                        "Compact retry %d/%d for session %s",
                        compact_retry_count, MAX_COMPACT_RETRIES, session_id,
                    )

                    # Disconnect old client
                    if client is not None:
                        try:
                            await client.disconnect()
                        except Exception:
                            pass

                    # Resume with new client (no query — just receive_response)
                    resume_id = new_claude_session_id or claude_session_id
                    client, _, sf = await self._get_or_create_client(
                        resume_id, compact_events=compact_events
                    )
                    if sf:
                        stderr_files.append(sf)
                    continue

                break  # No compact or retry limit reached

            # compact_end after successful retry completion
            if compact_retry_count > 0:
                yield {"type": "compact_end", "retries_used": compact_retry_count}

        except Exception as exc:
            logger.exception("Error during SDK stream")
            session_tag = claude_session_id or "new"
            stderr_path = self._stderr_log_dir / f"cli_stderr_{session_tag}.log"
            if stderr_path.exists():
                stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
                if stderr_text:
                    logger.error("CLI stderr output:\n%s", stderr_text[-2000:])
            yield {"type": "error", "error": str(exc)}
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            if claude_session_id and claude_session_id in self._clients:
                del self._clients[claude_session_id]
            return

        finally:
            for f in stderr_files:
                try:
                    f.close()
                except Exception:
                    pass

        # Cache the client under the (possibly new) claude session ID.
        if client is not None:
            if new_claude_session_id:
                if claude_session_id and claude_session_id != new_claude_session_id:
                    self._clients.pop(claude_session_id, None)
                self._clients[new_claude_session_id] = client
            elif claude_session_id not in self._clients:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        # Persist session metadata after successful stream
        last_text = "".join(last_text_parts)
        self._update_session_after_stream(session_id, new_claude_session_id, last_text)

        yield {"type": "message_end", "session_id": session_id}

    async def shutdown(self) -> None:
        """Disconnect all cached SDK clients. Called on server shutdown."""
        for cid, client in self._clients.items():
            try:
                await client.disconnect()
            except Exception:
                logger.debug("Failed to disconnect client %s", cid)
        self._clients.clear()
        logger.info("All Claude SDK clients disconnected")

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _make_session() -> dict:
        """Create a fresh session dict with a new UUID."""
        now = datetime.now(timezone.utc).isoformat()
        return {
            "id": str(uuid.uuid4()),
            "claude_session_id": None,
            "created_at": now,
            "last_active_at": now,
            "preview": None,
        }

    def _find_session(self, session_id: str) -> dict | None:
        for s in self._data["sessions"]:
            if s["id"] == session_id:
                return s
        return None

    def _build_compact_hook(self, compact_events: list) -> dict:
        """Build a PreCompact hook that records compact events."""
        async def on_pre_compact(
            hook_input: dict,
            tool_use_id: str | None,
            context: HookContext,
        ) -> HookJSONOutput:
            trigger = hook_input.get("trigger", "auto")
            logger.info(f"PreCompact hook triggered: {trigger}")
            compact_events.append({"trigger": trigger})
            return HookJSONOutput()

        return {
            "PreCompact": [HookMatcher(matcher=None, hooks=[on_pre_compact])]
        }

    def _build_options(
        self, claude_session_id: str | None, compact_events: list | None = None
    ) -> tuple[ClaudeAgentOptions, "IO[str]"]:
        """Build ClaudeAgentOptions for a session.

        Uses setting_sources=['project'] so the SDK reads .mcp.json from
        the workspace cwd automatically, eliminating the need for a separate
        --mcp-config flag.

        stderr is captured to a per-session log file via debug_stderr +
        extra_args={"debug-to-stderr": None}, matching the soulstream /
        rescuebot pattern. This ensures CLI output is on disk before any
        ProcessError is raised.
        """
        session_tag = claude_session_id or "new"
        stderr_path = self._stderr_log_dir / f"cli_stderr_{session_tag}.log"
        stderr_file = open(stderr_path, "a", encoding="utf-8")

        hooks = None
        if compact_events is not None:
            hooks = self._build_compact_hook(compact_events)

        opts = ClaudeAgentOptions(
            cwd=self._workspace_path,
            permission_mode="bypassPermissions",
            allowed_tools=[
                "Read", "Glob", "Grep", "Bash", "Edit", "Write",
                "WebFetch", "WebSearch", "Task", "ToolSearch", "Skill",
            ],
            disallowed_tools=["NotebookEdit", "TodoWrite"],
            setting_sources=["project"],
            extra_args={"debug-to-stderr": None},
            debug_stderr=stderr_file,
            hooks=hooks,
        )
        if claude_session_id:
            opts.resume = claude_session_id
        return opts, stderr_file

    async def _get_or_create_client(
        self,
        claude_session_id: str | None,
        compact_events: list | None = None,
    ) -> tuple[ClaudeSDKClient, bool, "IO[str] | None"]:
        """Return a live SDK client, reusing a cached one if available.

        Returns:
            (client, resumed, stderr_file) — caller must close stderr_file.
            stderr_file is None when returning a cached client.
        """
        if claude_session_id and claude_session_id in self._clients:
            return self._clients[claude_session_id], True, None

        # Try resume first
        if claude_session_id:
            try:
                opts, stderr_file = self._build_options(claude_session_id, compact_events)
                client = ClaudeSDKClient(opts)
                await client.connect()
                return client, True, stderr_file
            except Exception:
                logger.warning(
                    "Resume failed for session %s, falling back to new session",
                    claude_session_id,
                )
                self._clients.pop(claude_session_id, None)
                stderr_file.close()

        # New session (no resume)
        opts, stderr_file = self._build_options(None, compact_events)
        client = ClaudeSDKClient(opts)
        await client.connect()
        return client, False, stderr_file

    def _write_mcp_config(self) -> None:
        """Write .mcp.json into the workspace directory.

        The SDK with setting_sources=['project'] reads cwd/.mcp.json
        automatically. Only the haniel MCP server entry is needed here;
        the workspace-level .mcp.json (for soulstream sessions) is separate.
        """
        if self.runner.config.mcp is None:
            return

        mcp_port = self.runner.config.mcp.port
        config = {
            "mcpServers": {
                "haniel": {
                    "type": "http",
                    "url": f"http://localhost:{mcp_port}/mcp/http",
                }
            }
        }
        path = self._workspace_path / ".mcp.json"
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        logger.info("MCP config written to %s (port %d)", path, mcp_port)

    def _load_sessions(self) -> None:
        """Load session metadata from disk, or initialise if missing."""
        if self._sessions_path.exists():
            try:
                self._data = json.loads(self._sessions_path.read_text(encoding="utf-8"))
                logger.debug(
                    "Loaded %d sessions from %s",
                    len(self._data.get("sessions", [])),
                    self._sessions_path,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to load sessions file (%s): %s — starting fresh",
                    self._sessions_path,
                    exc,
                )
                self._data = {"sessions": [], "last_session_id": None}
        else:
            self._data = {"sessions": [], "last_session_id": None}

    def _save_sessions(self) -> None:
        """Persist session metadata to disk."""
        try:
            self._sessions_path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Failed to save sessions: %s", exc)

    def _update_session_after_stream(
        self,
        session_id: str | None,
        claude_session_id: str | None,
        last_text: str,
    ) -> None:
        """Update session metadata after a successful stream and persist."""
        if session_id is None:
            return
        session = self._find_session(session_id)
        if session is None:
            return

        if claude_session_id:
            session["claude_session_id"] = claude_session_id

        session["last_active_at"] = datetime.now(timezone.utc).isoformat()
        session["preview"] = last_text.replace("\n", " ")[:80] if last_text else None

        self._save_sessions()
