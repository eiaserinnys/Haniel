"""
Claude Code session manager for haniel dashboard chat panel.

Manages session metadata persistence and subprocess-based streaming
to the Claude CLI via `claude -p` / `claude --resume`.
"""

import asyncio
import copy
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, TYPE_CHECKING

if TYPE_CHECKING:
    from .runner import ServiceRunner

logger = logging.getLogger(__name__)

# Metadata file stored in runner.config_dir
_SESSIONS_FILE = "chat_sessions.json"
# MCP config written once at startup
_MCP_CONFIG_FILE = "haniel_mcp_config.json"


class ClaudeSessionManager:
    """Manages Claude Code subprocess sessions for the dashboard chat panel.

    Session metadata is persisted to disk at runner.config_dir/chat_sessions.json.
    Each session maps a haniel UUID to a Claude CLI session ID (used with --resume).

    Usage::

        manager = ClaudeSessionManager(runner)
        async for event in manager.stream_message(None, "hello"):
            # event: {"type": "text_delta", "delta": "..."}
            #        {"type": "session_id", "session_id": "<uuid>", "claude_session_id": "..."}
            #        {"type": "message_end"}
            #        {"type": "error", "error": "..."}
            ...
    """

    def __init__(self, runner: "ServiceRunner", claude_path: str | None = None):
        self.runner = runner
        self._sessions_path = runner.config_dir / _SESSIONS_FILE
        self._mcp_config_path: Path | None = None
        self._data: dict = {"sessions": [], "last_session_id": None}
        self._claude_exe = claude_path or "claude"

        self._load_sessions()
        self._mcp_config_path = self._write_mcp_config()

    # ── Public API ────────────────────────────────────────────────────────────

    def list_sessions(self) -> list[dict]:
        """Return a deep copy of all sessions (safe to iterate; won't mutate internal state)."""
        return copy.deepcopy(self._data["sessions"])

    def create_session(self) -> str:
        """Create and register a new session, returning its haniel UUID.

        Used by the chat panel's new_session command so the UUID returned
        to the client is immediately usable for subsequent send_message calls.
        """
        session_id = str(uuid.uuid4())
        session = {
            "id": session_id,
            "claude_session_id": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_active_at": datetime.now(timezone.utc).isoformat(),
            "preview": None,
        }
        self._data["sessions"].append(session)
        self._data["last_session_id"] = session_id
        self._save_sessions()
        return session_id

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
            session_id = str(uuid.uuid4())
            session = {
                "id": session_id,
                "claude_session_id": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "last_active_at": datetime.now(timezone.utc).isoformat(),
                "preview": None,
            }
            self._data["sessions"].append(session)

        self._data["last_session_id"] = session_id

        claude_session_id: str | None = session.get("claude_session_id")
        cmd = self._build_command(claude_session_id, text)

        yield {"type": "session_start", "session_id": session_id, "is_new": is_new}

        last_text_parts: list[str] = []
        new_claude_session_id: str | None = None

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                # Discard stderr to avoid filling the pipe buffer while we only
                # consume stdout.  Error info comes through stdout stream-json.
                stderr=asyncio.subprocess.DEVNULL,
            )

            try:
                assert proc.stdout is not None
                async for raw_line in proc.stdout:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type")

                    if event_type == "text":
                        delta = event.get("text", "")
                        last_text_parts.append(delta)
                        yield {"type": "text_delta", "delta": delta}

                    elif event_type == "result":
                        new_claude_session_id = event.get("session_id")

                await proc.wait()
            finally:
                # Ensure subprocess is terminated if the generator is abandoned
                # (e.g. WebSocket closed mid-stream).
                if proc.returncode is None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass

            if proc.returncode != 0:
                yield {
                    "type": "error",
                    "error": f"claude exited with code {proc.returncode}",
                }
                return

        except FileNotFoundError:
            yield {"type": "error", "error": "claude CLI not found in PATH"}
            return
        except Exception as exc:
            logger.exception("Unexpected error while running claude subprocess")
            yield {"type": "error", "error": str(exc)}
            return

        # Persist session metadata after successful stream
        last_text = "".join(last_text_parts)
        self._update_session_after_stream(session_id, new_claude_session_id, last_text)

        yield {"type": "message_end", "session_id": session_id}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _find_session(self, session_id: str) -> dict | None:
        for s in self._data["sessions"]:
            if s["id"] == session_id:
                return s
        return None

    def _build_command(
        self,
        claude_session_id: str | None,
        text: str,
    ) -> list[str]:
        """Build the claude CLI command for a message.

        Four variants (mcp_active × has_session):
          No session, MCP active:   ['claude', '-p', text, '--output-format', 'stream-json', '--mcp-config', path]
          No session, MCP inactive: ['claude', '-p', text, '--output-format', 'stream-json']
          With session, MCP active: ['claude', '--resume', id, '-p', text, '--output-format', 'stream-json', '--mcp-config', path]
          With session, MCP inactive: ['claude', '--resume', id, '-p', text, '--output-format', 'stream-json']
        """
        mcp_active = self._mcp_config_path is not None

        if claude_session_id:
            cmd = [
                self._claude_exe,
                "--resume",
                claude_session_id,
                "-p",
                text,
                "--output-format",
                "stream-json",
                "--verbose",
            ]
        else:
            cmd = [
                self._claude_exe,
                "-p",
                text,
                "--output-format",
                "stream-json",
                "--verbose",
            ]

        if mcp_active:
            cmd += ["--mcp-config", str(self._mcp_config_path)]

        return cmd

    def _write_mcp_config(self) -> Path | None:
        """Write MCP config file for claude subprocess, once at startup.

        Returns None if runner.config.mcp is None (MCP disabled → no --mcp-config flag).
        """
        if self.runner.config.mcp is None:
            return None

        mcp_port = self.runner.config.mcp.port
        config = {
            "mcpServers": {
                "haniel": {
                    "transport": {
                        "type": "http",
                        "url": f"http://localhost:{mcp_port}/mcp/http",
                    }
                }
            }
        }
        path = self.runner.config_dir / _MCP_CONFIG_FILE
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        logger.info("MCP config written to %s (port %d)", path, mcp_port)
        return path

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
