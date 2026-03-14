"""
Interactive installer - Phase 2.

Handles Claude Code integration for interactive configuration:
- Uses Claude Code SDK to run a structured conversation
- Claude analyzes config status and returns structured JSON responses
- haniel manages the conversation loop, applies config values, and finalizes

Claude is a read-only analyzer: it can inspect install status and config details
via MCP tools, but cannot set values or finalize. haniel handles all mutations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .install_mcp_server import InstallMcpServer

from ..config import HanielConfig
from .state import InstallState, InstallPhase

logger = logging.getLogger(__name__)

# Timeout for waiting for Claude Code to finish (in seconds)
CLAUDE_CODE_TIMEOUT = 3600  # 1 hour max
# Poll interval for checking finalize status (in seconds)
FINALIZE_POLL_INTERVAL = 1.0

# SDK availability flag
try:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False


class InteractiveInstaller:
    """Handles interactive installation via Claude Code."""

    def __init__(
        self,
        config: HanielConfig,
        config_dir: Path,
        state: InstallState,
    ):
        self.config = config
        self.config_dir = config_dir
        self.state = state
        self._finalize_requested = False
        self._mcp_server: Optional["InstallMcpServer"] = None

    def has_pending_configs(self) -> bool:
        """Check if there are pending configs requiring user input."""
        if not self.config.install or not self.config.install.configs:
            return False

        for name, cfg in self.config.install.configs.items():
            if cfg.keys:
                for key_cfg in cfg.keys:
                    if key_cfg.key not in self.state.config_values.get(name, {}):
                        if not key_cfg.default:
                            return True

        return False

    def get_install_status(self) -> dict[str, Any]:
        """Get the current installation status.

        MCP tool: haniel_install_status() (read-only)
        """
        pending_configs: list[dict[str, Any]] = []

        if self.config.install and self.config.install.configs:
            for name, cfg in self.config.install.configs.items():
                if cfg.keys:
                    filled_keys: list[str] = []
                    missing_keys: list[str] = []

                    for key_cfg in cfg.keys:
                        if key_cfg.key in self.state.config_values.get(name, {}):
                            filled_keys.append(key_cfg.key)
                        elif key_cfg.default:
                            filled_keys.append(key_cfg.key)
                        else:
                            missing_keys.append(key_cfg.key)

                    if missing_keys:
                        pending_configs.append(
                            {
                                "name": name,
                                "path": cfg.path,
                                "missing_keys": missing_keys,
                                "filled_keys": filled_keys,
                            }
                        )

        return {
            "phase": self.state.phase.value,
            "completed": self.state.completed_steps,
            "failed": [
                {"step": s.step, "error": s.error, "timestamp": s.timestamp}
                for s in self.state.failed_steps
            ],
            "pending_configs": pending_configs,
            "config_values_count": sum(
                len(v) for v in self.state.config_values.values()
            ),
        }

    def set_config(self, config_name: str, key: str, value: str) -> dict[str, Any]:
        """Set a config value. Called by haniel directly, not exposed via MCP."""
        if not self.config.install or not self.config.install.configs:
            return {"success": False, "error": "No configs defined"}

        if config_name not in self.config.install.configs:
            return {"success": False, "error": f"Unknown config: {config_name}"}

        cfg = self.config.install.configs[config_name]
        if not cfg.keys:
            return {"success": False, "error": f"Config {config_name} has no keys"}

        key_cfg = None
        for k in cfg.keys:
            if k.key == key:
                key_cfg = k
                break

        if key_cfg is None:
            return {"success": False, "error": f"Unknown key: {key}"}

        self.state.set_config_value(config_name, key, value)

        if config_name in self.state.pending_configs:
            if key in self.state.pending_configs[config_name]:
                self.state.pending_configs[config_name].remove(key)
            if not self.state.pending_configs[config_name]:
                del self.state.pending_configs[config_name]

        return {"success": True, "config": config_name, "key": key}

    def get_config(self, config_name: str) -> dict[str, Any]:
        """Get the status of a specific config.

        MCP tool: haniel_get_config(config_name) (read-only)
        """
        if not self.config.install or not self.config.install.configs:
            return {"error": "No configs defined"}

        if config_name not in self.config.install.configs:
            return {"error": f"Unknown config: {config_name}"}

        cfg = self.config.install.configs[config_name]
        if not cfg.keys:
            return {"error": f"Config {config_name} has no keys"}

        filled_keys: list[str] = []
        missing_keys: list[str] = []
        key_details: list[dict[str, Any]] = []

        for key_cfg in cfg.keys:
            detail: dict[str, Any] = {
                "key": key_cfg.key,
                "prompt": key_cfg.prompt,
                "guide": key_cfg.guide,
                "description": key_cfg.description,
                "has_default": key_cfg.default is not None,
            }

            if key_cfg.key in self.state.config_values.get(config_name, {}):
                filled_keys.append(key_cfg.key)
                detail["status"] = "filled"
            elif key_cfg.default:
                filled_keys.append(key_cfg.key)
                detail["status"] = "default"
                detail["default_value"] = key_cfg.default
            else:
                missing_keys.append(key_cfg.key)
                detail["status"] = "missing"

            key_details.append(detail)

        return {
            "name": config_name,
            "path": cfg.path,
            "filled_keys": filled_keys,
            "missing_keys": missing_keys,
            "keys": key_details,
        }

    def retry_step(self, step_name: str) -> dict[str, Any]:
        """Retry a failed step."""
        from .orchestrator import InstallOrchestrator

        orchestrator = InstallOrchestrator(self.config, self.config_dir, self.state)
        return orchestrator.retry_step(step_name)

    def finalize_install(self) -> dict[str, Any]:
        """Signal that installation should be finalized.

        Called by haniel directly after sufficient: true, not exposed via MCP.
        """
        status = self.get_install_status()

        for pending in status["pending_configs"]:
            if pending["missing_keys"]:
                return {
                    "success": False,
                    "error": f"Missing keys in {pending['name']}: {pending['missing_keys']}",
                }

        self._finalize_requested = True
        self.state.transition_to(InstallPhase.FINALIZE)

        return {"success": True, "message": "Finalization requested"}

    def is_finalize_requested(self) -> bool:
        return self._finalize_requested

    def get_claude_prompt(self) -> str:
        """Generate the system prompt for the Claude Code SDK session.

        Includes system context, config key details with descriptions,
        structured response format, and conversation strategy guide.
        """
        status = self.get_install_status()

        # Build config info with descriptions
        config_info: list[str] = []
        if self.config.install and self.config.install.configs:
            for name, cfg in self.config.install.configs.items():
                if cfg.keys:
                    config_info.append(f"\n### {name} ({cfg.path})")
                    for key_cfg in cfg.keys:
                        parts = [f"- **{key_cfg.key}**"]
                        if key_cfg.description:
                            parts.append(f": {key_cfg.description}")
                        if key_cfg.default:
                            parts.append(f" (default: `{key_cfg.default}`)")
                        if key_cfg.prompt:
                            parts.append(f"\n  - prompt: {key_cfg.prompt}")
                        if key_cfg.guide:
                            parts.append(f"\n  - guide: {key_cfg.guide}")
                        config_info.append("".join(parts))

        return f"""당신은 haniel 설치 도우미입니다.
사용자와 대화하면서 서비스 실행에 필요한 설정 값을 수집해주세요.

## 시스템 개요

soulstream은 Claude Code 원격 실행 서비스입니다.
- **soul-server**: FastAPI 백엔드. Claude Code 세션을 생성하고 관리하며, 러너 풀로 동시 세션을 처리합니다.
- **soul-dashboard**: Express + React 웹 대시보드. soul-server에 연결하여 세션을 모니터링합니다.

haniel은 이 서비스들의 프로세스 관리자이며, 지금 설치를 진행하고 있습니다.

## 현재 설치 상태
{json.dumps(status, indent=2, ensure_ascii=False)}

## 수집할 설정들
{"".join(config_info)}

## 응답 형식 (필수)

반드시 아래 형식의 JSON을 ```json 코드블록으로 반환하세요.
JSON 외의 텍스트는 message 필드 안에 넣으세요.

```json
{{
  "message": "사용자에게 보여줄 안내 메시지나 질문",
  "to_set": [
    {{"config": "config-name", "key": "KEY_NAME", "value": "value"}}
  ],
  "sufficient": false
}}
```

- **message**: 사용자에게 표시할 텍스트 (질문, 안내, 확인 요청)
- **to_set**: 이번 턴에서 확정된 설정값. 비어있을 수 있음
- **sufficient**: 모든 필수 설정이 수집되었으면 true

## 대화 전략

1. 먼저 기본값이 있는 키들을 일괄 확인하세요 ("다음 기본값을 사용합니다. 변경이 필요한 항목이 있으면 알려주세요.")
   - 기본값 키들은 사용자가 변경을 원하지 않으면 to_set에 기본값으로 넣으세요
2. 자동 생성 가능한 값(AUTH_BEARER_TOKEN 등)은 생성하여 사용자에게 확인을 받으세요
3. 사용자 입력이 필요한 키(WORKSPACE_DIR, DASH_USER_NAME 등)를 질문하세요
4. 한 번에 2-3개씩 질문하세요. 너무 많은 질문을 한꺼번에 하지 마세요
5. 모든 키가 확정되면 설정 요약을 보여주고 sufficient: true로 응답하세요
"""

    def launch_claude_code_session(self) -> bool:
        """Launch a Claude Code SDK session for interactive config collection.

        Uses ClaudeSDKClient for structured conversation. Claude returns
        JSON responses with {message, to_set, sufficient} on each turn.
        haniel manages the conversation loop, applies config values,
        and finalizes when sufficient: true is received.

        Must be called from a synchronous context (not inside an existing
        asyncio event loop). Uses asyncio.run() internally.

        Returns:
            True if session completed successfully
        """
        if not SDK_AVAILABLE:
            logger.error(
                "claude-agent-sdk is not installed. "
                "Install it with: pip install claude-agent-sdk"
            )
            return False

        from .install_mcp_server import InstallMcpServer

        mcp_port = self._get_install_mcp_port()

        self._mcp_server = InstallMcpServer(self, port=mcp_port)
        self._mcp_server.start_background()

        try:
            return asyncio.run(
                asyncio.wait_for(self._run_sdk_session(), timeout=CLAUDE_CODE_TIMEOUT)
            )
        except asyncio.TimeoutError:
            logger.error("Interactive session timed out")
            return False
        except Exception as e:
            if self._finalize_requested:
                logger.warning(
                    f"SDK session cleanup error (ignored, finalize already done): {e}"
                )
                return True
            logger.error(f"SDK session failed: {e}")
            return False
        finally:
            if self._mcp_server:
                self._mcp_server.stop_background()
                self._mcp_server = None

    async def _run_sdk_session(self) -> bool:
        """Run the SDK-based interactive conversation loop.

        Each turn: Claude responds with structured JSON, haniel applies
        to_set values, displays message, reads user input, and sends
        it back to Claude. Loop ends when sufficient: true.
        """
        mcp_port = self._get_install_mcp_port()
        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=str(self.config_dir),
            mcp_servers={
                "haniel": {
                    "type": "sse",
                    "url": f"http://localhost:{mcp_port}/sse",
                }
            },
            max_turns=20,
        )

        client = ClaudeSDKClient(options=options)
        await client.connect()

        try:
            logger.info("Starting interactive SDK session...")
            await client.query(self.get_claude_prompt())

            while True:
                response = await self._receive_structured_response(client)
                message = response.get("message", "")
                if message:
                    print(message)
                self._apply_to_set(response.get("to_set", []))
                if response.get("sufficient", False):
                    self.finalize_install()
                    break
                try:
                    user_input = input("\n> ")
                except (EOFError, KeyboardInterrupt):
                    logger.info("User interrupted interactive session")
                    return False
                await client.query(user_input)

            logger.info("All configs collected, session complete")
            return True

        finally:
            await client.disconnect()

    async def _receive_structured_response(self, client: Any) -> dict:
        """Receive Claude's response and extract structured JSON.

        Collects all TextBlock content from AssistantMessage events,
        then extracts JSON from a ```json code block or raw JSON.

        Args:
            client: ClaudeSDKClient instance

        Returns:
            Parsed JSON dict with message, to_set, sufficient fields
        """
        full_text = ""
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        full_text += block.text
            elif isinstance(msg, ResultMessage):
                break

        # Extract JSON from ```json ... ``` code block
        match = re.search(r"```json\s*(.*?)\s*```", full_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # Try parsing the entire text as JSON
        try:
            return json.loads(full_text.strip())
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse structured response: {full_text[:200]}")
            return {"message": full_text, "to_set": [], "sufficient": False}

    def _apply_to_set(self, to_set: list[dict]) -> None:
        """Apply confirmed config values from Claude's response.

        Args:
            to_set: List of {config, key, value} dicts
        """
        for item in to_set:
            config_name = item.get("config", "")
            key = item.get("key", "")
            value = item.get("value", "")
            if config_name and key:
                result = self.set_config(config_name, key, value)
                if result.get("success"):
                    logger.info(f"Set {config_name}.{key}")
                else:
                    logger.warning(
                        f"Failed to set {config_name}.{key}: {result.get('error')}"
                    )

    def run_headless_session(self, timeout: float = 300.0) -> bool:
        """Run an interactive session in headless mode (for testing).

        This starts the MCP server but doesn't launch Claude Code,
        allowing tests to interact with the MCP tools directly.
        """
        from .install_mcp_server import InstallMcpServer

        mcp_port = self._get_install_mcp_port()

        try:
            logger.info(f"Starting headless install MCP server on port {mcp_port}...")
            self._mcp_server = InstallMcpServer(self, port=mcp_port)
            self._mcp_server.start_background()

            start_time = time.time()
            while not self._finalize_requested:
                if time.time() - start_time > timeout:
                    logger.error("Headless session timed out waiting for finalize")
                    return False
                time.sleep(FINALIZE_POLL_INTERVAL)

            logger.info("Headless session finalized successfully")
            return True

        except Exception as e:
            logger.error(f"Headless session error: {e}")
            return False

        finally:
            if self._mcp_server:
                self._mcp_server.stop_background()
                self._mcp_server = None

    def _get_install_mcp_port(self) -> int:
        """Get the port for install-mode MCP server."""
        if self.config.mcp:
            return self.config.mcp.port + 1
        return 3201

    # MCP tool definitions — read-only tools only

    def get_mcp_tools(self) -> list[dict[str, Any]]:
        """Get MCP tool definitions for install mode.

        Only read-only tools are exposed. Write operations (set_config,
        finalize_install) are handled by haniel directly.
        """
        return [
            {
                "name": "haniel_install_status",
                "description": "Get the current installation status including completed steps, failed steps, and pending configs",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "haniel_get_config",
                "description": "Get the status of a specific config including filled and missing keys with descriptions",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "config_name": {
                            "type": "string",
                            "description": "Name of the config to get status for",
                        },
                    },
                    "required": ["config_name"],
                },
            },
        ]

    async def call_mcp_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Handle MCP tool calls for install mode (read-only)."""
        if name == "haniel_install_status":
            result = self.get_install_status()
        elif name == "haniel_get_config":
            result = self.get_config(arguments.get("config_name", ""))
        else:
            result = {"error": f"Unknown tool: {name}"}

        return json.dumps(result, ensure_ascii=False, indent=2)
