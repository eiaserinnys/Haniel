"""
Interactive installer - Phase 2.

Handles Claude Code integration for interactive configuration:
- Provides MCP tools for Claude Code to use
- Launches Claude Code session
- Manages config value collection

haniel doesn't care what Claude Code does - it just provides tools
and waits for finalize signal.
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from ..config import HanielConfig
from .state import InstallState, InstallPhase

logger = logging.getLogger(__name__)


class InteractiveInstaller:
    """Handles interactive installation via Claude Code."""

    def __init__(
        self,
        config: HanielConfig,
        config_dir: Path,
        state: InstallState,
    ):
        """Initialize the interactive installer.

        Args:
            config: Haniel configuration
            config_dir: Directory containing haniel.yaml
            state: Installation state
        """
        self.config = config
        self.config_dir = config_dir
        self.state = state
        self._finalize_requested = False

    def has_pending_configs(self) -> bool:
        """Check if there are pending configs requiring user input.

        Returns:
            True if there are pending configs
        """
        if not self.config.install or not self.config.install.configs:
            return False

        for name, cfg in self.config.install.configs.items():
            if cfg.keys:
                for key_cfg in cfg.keys:
                    # Check if key is missing and has no default
                    if key_cfg.key not in self.state.config_values.get(name, {}):
                        if not key_cfg.default:
                            return True

        return False

    def get_install_status(self) -> dict[str, Any]:
        """Get the current installation status.

        MCP tool: haniel_install_status()

        Returns:
            Status dict with phase, completed, failed, and pending_configs
        """
        # Build pending configs list
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
                            # Has default, will be auto-filled
                            filled_keys.append(key_cfg.key)
                        else:
                            missing_keys.append(key_cfg.key)

                    if missing_keys:
                        pending_configs.append({
                            "name": name,
                            "path": cfg.path,
                            "missing_keys": missing_keys,
                            "filled_keys": filled_keys,
                        })

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
        """Set a config value.

        MCP tool: haniel_set_config(config_name, key, value)

        Args:
            config_name: Name of the config (e.g., "workspace-env")
            key: Key name
            value: Value to set

        Returns:
            Result dict with success status
        """
        # Validate config exists
        if not self.config.install or not self.config.install.configs:
            return {"success": False, "error": "No configs defined"}

        if config_name not in self.config.install.configs:
            return {"success": False, "error": f"Unknown config: {config_name}"}

        cfg = self.config.install.configs[config_name]
        if not cfg.keys:
            return {"success": False, "error": f"Config {config_name} has no keys"}

        # Validate key exists
        key_cfg = None
        for k in cfg.keys:
            if k.key == key:
                key_cfg = k
                break

        if key_cfg is None:
            return {"success": False, "error": f"Unknown key: {key}"}

        # Set the value
        self.state.set_config_value(config_name, key, value)

        # Update pending configs
        if config_name in self.state.pending_configs:
            if key in self.state.pending_configs[config_name]:
                self.state.pending_configs[config_name].remove(key)
            if not self.state.pending_configs[config_name]:
                del self.state.pending_configs[config_name]

        return {"success": True, "config": config_name, "key": key}

    def get_config(self, config_name: str) -> dict[str, Any]:
        """Get the status of a specific config.

        MCP tool: haniel_get_config(config_name)

        Args:
            config_name: Name of the config

        Returns:
            Config status with filled and missing keys
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
        """Retry a failed step.

        MCP tool: haniel_retry_step(step_name)

        Args:
            step_name: Name of the step to retry

        Returns:
            Result dict with success status
        """
        # This delegates to orchestrator, but we need to handle it here
        # for the MCP interface
        from .orchestrator import InstallOrchestrator

        orchestrator = InstallOrchestrator(
            self.config, self.config_dir, self.state
        )
        return orchestrator.retry_step(step_name)

    def finalize_install(self) -> dict[str, Any]:
        """Signal that installation should be finalized.

        MCP tool: haniel_finalize_install()

        Returns:
            Result dict with success status
        """
        # Check all required configs are filled
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
        """Check if finalize has been requested.

        Returns:
            True if finalize was requested
        """
        return self._finalize_requested

    def get_claude_prompt(self) -> str:
        """Generate the prompt for Claude Code session.

        Returns:
            Prompt string
        """
        status = self.get_install_status()

        # Build config info
        config_info: list[str] = []
        if self.config.install and self.config.install.configs:
            for name, cfg in self.config.install.configs.items():
                if cfg.keys:
                    config_info.append(f"\n### {name} ({cfg.path})")
                    for key_cfg in cfg.keys:
                        prompt = key_cfg.prompt or key_cfg.key
                        guide = f" - Guide: {key_cfg.guide}" if key_cfg.guide else ""
                        default = (
                            f" (default: {key_cfg.default})" if key_cfg.default else ""
                        )
                        config_info.append(f"- {key_cfg.key}: {prompt}{default}{guide}")

        return f"""당신은 haniel 설치 도우미입니다.
사용자와 대화하면서 서비스 실행에 필요한 설정 값을 수집해주세요.

## 현재 상태
{json.dumps(status, indent=2, ensure_ascii=False)}

## 수집할 설정들
{"".join(config_info)}

## 사용 가능한 도구

haniel MCP 도구를 사용하여:
1. `haniel_install_status()` - 현재 상태를 확인하세요
2. 실패한 단계가 있으면 `haniel_retry_step(step_name)`으로 재시도
3. 각 missing_keys에 대해:
   - 값의 용도와 발급/확인 방법을 안내하세요 (guide 필드 참고)
   - 사용자에게 값을 물어보세요
   - `haniel_set_config(config_name, key, value)`로 값을 설정하세요
4. 모든 값이 채워지면 `haniel_finalize_install()`을 호출하세요

## 참고사항

- default가 있는 키는 "기본값 {{default}}을 사용할까요?"라고 물어보세요
- 사용자가 잘 모르면 발급 방법을 친절히 안내해주세요
- 값이 잘못된 것 같으면 재입력을 요청하세요
"""

    def launch_claude_code_session(self) -> bool:
        """Launch a Claude Code session for interactive config.

        Returns:
            True if session completed successfully
        """
        prompt = self.get_claude_prompt()

        # Create a temporary MCP config for the install session
        mcp_config = {
            "mcpServers": {
                "haniel": {
                    "url": f"http://localhost:{self._get_install_mcp_port()}/sse"
                }
            }
        }

        mcp_config_path = self.config_dir / ".haniel-install-mcp.json"
        mcp_config_path.write_text(json.dumps(mcp_config, indent=2))

        try:
            logger.info("Launching Claude Code session...")

            # Note: In practice, this would start the MCP server in the background
            # and then launch Claude Code. For now, we just log the intent.
            logger.info(f"Would run: claude -p --mcp-config {mcp_config_path}")
            logger.info(f"Prompt: {prompt[:200]}...")

            # In a real implementation:
            # 1. Start MCP server in background thread
            # 2. Run: subprocess.run(["claude", "-p", "--mcp-config", str(mcp_config_path)])
            # 3. Wait for finalize signal
            # 4. Stop MCP server

            # For now, return True (implementation will be completed when integrating)
            return True

        except Exception as e:
            logger.error(f"Failed to launch Claude Code: {e}")
            return False
        finally:
            # Cleanup
            if mcp_config_path.exists():
                mcp_config_path.unlink()

    def _get_install_mcp_port(self) -> int:
        """Get the port for install-mode MCP server.

        Returns:
            Port number
        """
        # Use a different port than runtime MCP
        if self.config.mcp:
            return self.config.mcp.port + 1
        return 3201

    # MCP tool definitions for registration

    def get_mcp_tools(self) -> list[dict[str, Any]]:
        """Get MCP tool definitions for install mode.

        Returns:
            List of tool definitions
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
                "name": "haniel_set_config",
                "description": "Set a configuration value for the installation",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "config_name": {
                            "type": "string",
                            "description": "Name of the config (e.g., 'workspace-env')",
                        },
                        "key": {
                            "type": "string",
                            "description": "Key name to set",
                        },
                        "value": {
                            "type": "string",
                            "description": "Value to set",
                        },
                    },
                    "required": ["config_name", "key", "value"],
                },
            },
            {
                "name": "haniel_get_config",
                "description": "Get the status of a specific config including filled and missing keys",
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
            {
                "name": "haniel_retry_step",
                "description": "Retry a failed installation step",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "step_name": {
                            "type": "string",
                            "description": "Name of the step to retry",
                        },
                    },
                    "required": ["step_name"],
                },
            },
            {
                "name": "haniel_finalize_install",
                "description": "Finalize the installation. Call this when all config values are collected.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    async def call_mcp_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> str:
        """Handle MCP tool calls for install mode.

        Args:
            name: Tool name
            arguments: Tool arguments

        Returns:
            Tool result as JSON string
        """
        if name == "haniel_install_status":
            result = self.get_install_status()
        elif name == "haniel_set_config":
            result = self.set_config(
                arguments.get("config_name", ""),
                arguments.get("key", ""),
                arguments.get("value", ""),
            )
        elif name == "haniel_get_config":
            result = self.get_config(arguments.get("config_name", ""))
        elif name == "haniel_retry_step":
            result = self.retry_step(arguments.get("step_name", ""))
        elif name == "haniel_finalize_install":
            result = self.finalize_install()
        else:
            result = {"error": f"Unknown tool: {name}"}

        return json.dumps(result, ensure_ascii=False, indent=2)
