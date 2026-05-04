"""
haniel configuration schema and parser.

This module defines Pydantic models for haniel.yaml configuration files.
haniel doesn't care what it runs - it just parses the config and validates structure.
"""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class ShutdownConfig(BaseModel):
    """Configuration for graceful shutdown behavior."""

    timeout: int = Field(
        default=10, description="Seconds to wait for graceful shutdown"
    )
    kill_timeout: int = Field(
        default=30, description="Seconds before SIGKILL after timeout"
    )
    signal: str = Field(
        default="SIGTERM", description="Signal to send for graceful shutdown"
    )
    method: str | None = Field(
        default=None, description="Shutdown method: None or 'http'"
    )
    endpoint: str | None = Field(
        default=None, description="HTTP endpoint for shutdown (if method='http')"
    )


class BackoffConfig(BaseModel):
    """Configuration for restart backoff and circuit breaker."""

    base_delay: int = Field(
        default=5, description="Initial delay before restart (seconds)"
    )
    max_delay: int = Field(
        default=300, description="Maximum delay between restarts (seconds)"
    )
    circuit_breaker: int = Field(
        default=5, description="Failures before circuit breaker trips"
    )
    circuit_window: int = Field(
        default=300, description="Time window for circuit breaker (seconds)"
    )


class WebhookConfig(BaseModel):
    """Configuration for notification webhooks."""

    url: str = Field(..., description="Webhook URL")
    format: str = Field(
        default="json", description="Webhook format: slack, discord, json"
    )


class DashboardConfig(BaseModel):
    """Configuration for the built-in web dashboard."""

    enabled: bool = Field(default=True, description="Whether dashboard is enabled")
    port: int | None = Field(
        default=None,
        description="Port for dashboard server. None means share MCP port.",
    )
    token: str | None = Field(
        default=None,
        description=(
            "Bearer token for API/WebSocket access. "
            "If set, all requests must include 'Authorization: Bearer <token>'. "
            "If None, dashboard is accessible without authentication (warning logged)."
        ),
    )


class McpConfig(BaseModel):
    """Configuration for MCP server."""

    enabled: bool = Field(default=True, description="Whether MCP server is enabled")
    transport: str = Field(
        default="streamable_http", description="Transport type: streamable_http, stdio"
    )
    port: int = Field(
        default=3200, description="Port for MCP server (if transport=sse)"
    )


class HooksConfig(BaseModel):
    """Configuration for lifecycle hooks."""

    post_pull: str | None = Field(
        default=None, description="Command to run after git pull"
    )
    pre_start: str | None = Field(
        default=None, description="Command to run before service start"
    )


class RepoConfig(BaseModel):
    """Configuration for a git repository."""

    url: str = Field(..., description="Git clone URL")
    branch: str = Field(default="main", description="Branch to track")
    path: str = Field(..., description="Local path for the repository")
    hooks: HooksConfig | None = Field(default=None, description="Lifecycle hooks")
    pull_strategy: Literal["merge", "force"] | None = Field(
        default=None,
        description="Pull 전략. 'force'이면 git reset --hard로 로컬 변경을 드롭. 기본값 None은 'merge'(기존 git pull)와 동일.",
    )


class ServiceShutdownConfig(BaseModel):
    """Per-service shutdown configuration (overrides global)."""

    signal: str = Field(default="SIGTERM", description="Signal to send")
    timeout: int = Field(default=10, description="Seconds to wait")
    method: str | None = Field(default=None, description="Shutdown method")
    endpoint: str | None = Field(default=None, description="HTTP endpoint")


class ServiceConfig(BaseModel):
    """Configuration for a service."""

    run: str = Field(..., description="Command to execute")
    cwd: str | None = Field(default=None, description="Working directory")
    repo: str | None = Field(
        default=None, description="Repository this service depends on"
    )
    restart_delay: int | None = Field(default=None, description="Delay before restart")
    after: list[str] = Field(
        default_factory=list, description="Services to wait for before starting"
    )
    ready: str | None = Field(
        default=None,
        description="Ready condition: port:N, delay:N, log:pattern, http:url",
    )
    shutdown: ServiceShutdownConfig | None = Field(
        default=None, description="Shutdown configuration"
    )
    enabled: bool = Field(default=True, description="Whether service is enabled")
    hooks: HooksConfig | None = Field(default=None, description="Lifecycle hooks")
    reflect: bool = Field(
        default=False,
        description="Whether this service exposes a /reflect endpoint for introspection",
    )

    @field_validator("after", mode="before")
    @classmethod
    def normalize_after(cls, v: str | list[str] | None) -> list[str]:
        """Convert single string to list for after field."""
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v


class EnvironmentConfig(BaseModel):
    """Configuration for a runtime environment (venv, npm, etc.)."""

    type: str = Field(..., description="Environment type: python-venv, npm, pnpm")
    path: str = Field(..., description="Path to environment directory")
    requirements: list[str] | None = Field(
        default=None, description="Requirements files (for python-venv)"
    )
    build: str | None = Field(
        default=None,
        description="Build command to run after install (e.g. 'pnpm run build')",
    )


class ConfigKeyConfig(BaseModel):
    """Configuration for a single config file key."""

    key: str = Field(..., description="Key name")
    prompt: str | None = Field(default=None, description="Prompt for interactive input")
    guide: str | None = Field(
        default=None, description="Guide for obtaining this value"
    )
    validate_cmd: str | None = Field(
        default=None, alias="validate", description="Validation command"
    )
    default: str | None = Field(default=None, description="Default value")
    description: str | None = Field(
        default=None, description="Human-readable description for AI-assisted setup"
    )


class ConfigFileConfig(BaseModel):
    """Configuration for a config file to be created during install."""

    path: str = Field(..., description="Path to config file")
    keys: list[ConfigKeyConfig] | None = Field(
        default=None, description="Keys for interactive config"
    )
    content: str | None = Field(default=None, description="Static content for the file")


class ServiceAccountConfig(BaseModel):
    """Windows service account credentials.

    When set, the service runs under this user account instead of LocalSystem.
    The user's profile is loaded automatically, so HOME / GIT_CONFIG_GLOBAL
    do not need to be set manually in environment.
    """

    username: str = Field(
        ...,
        description="Account to run the service as (e.g. '.\\\\username' for local accounts)",
    )
    password: str | None = Field(default=None, description="Account password")
    allow_service_logon: bool = Field(
        default=True,
        description="Grant 'Log on as a service' right to the account",
    )


class ServiceDefinitionConfig(BaseModel):
    """Configuration for system service registration (WinSW, systemd)."""

    name: str = Field(..., description="Service name")
    display: str | None = Field(default=None, description="Display name")
    working_directory: str = Field(default="{root}", description="Working directory")
    environment: dict[str, str] | None = Field(
        default=None, description="Environment variables"
    )
    service_account: ServiceAccountConfig | None = Field(
        default=None,
        description="Run service as a specific user account (default: LocalSystem)",
    )


class InstallConfig(BaseModel):
    """Configuration for the install phase."""

    requirements: dict[str, Any] | None = Field(
        default=None, description="System requirements"
    )
    directories: list[str] | None = Field(
        default=None, description="Directories to create"
    )
    environments: dict[str, EnvironmentConfig] | None = Field(
        default=None, description="Runtime environments"
    )
    configs: dict[str, ConfigFileConfig] | None = Field(
        default=None, description="Config files to create"
    )
    service: ServiceDefinitionConfig | None = Field(
        default=None, description="System service registration"
    )


class SelfUpdateConfig(BaseModel):
    """Configuration for haniel self-update mechanism.

    When configured, haniel polls its own repo for changes and can
    update itself via exit code signaling to the wrapper script.
    See ADR-0002 for architecture details.
    """

    repo: str = Field(
        ..., description="Key from repos section identifying haniel's own repo"
    )
    auto_update: bool = Field(
        default=False, description="If true, update immediately without approval"
    )


class SlackBotConfig(BaseModel):
    """Configuration for the integrated Slack bot."""

    enabled: bool = Field(default=True, description="Whether to start the Slack bot")
    bot_token: str = Field(
        ..., description="Bot User OAuth Token (xoxb-...)"
    )
    app_token: str = Field(
        ..., description="App-Level Token for Socket Mode (xapp-...)"
    )
    notify_user: str = Field(
        ..., description="Slack User ID to send DMs to (U...)"
    )


class OrchestratorClientConfig(BaseModel):
    """Configuration for connecting to a remote orchestrator server."""

    enabled: bool = Field(default=True, description="Whether to connect to orchestrator")
    url: str = Field(
        ..., description="Orchestrator WebSocket URL (wss://host/ws/node)"
    )
    token: str = Field(
        ..., description="Authentication token — shared secret with OrchestratorConfig.token"
    )
    node_id: str = Field(
        ..., description="This node's identifier"
    )
    reconnect_base: float = Field(
        default=1.0, description="Base reconnect delay in seconds"
    )
    reconnect_max: float = Field(
        default=60.0, description="Max reconnect delay in seconds"
    )


class HanielConfig(BaseModel):
    """Root configuration for haniel.yaml."""

    auto_apply: bool = Field(
        default=True,
        description="If false, detected changes are shown in dashboard but not auto-applied. "
        "Manual Update from dashboard is still possible.",
    )
    poll_interval: int = Field(
        default=60, description="Seconds between git fetch polls"
    )
    shutdown: ShutdownConfig | None = Field(
        default=None, description="Global shutdown configuration"
    )
    backoff: BackoffConfig | None = Field(
        default=None, description="Backoff and circuit breaker configuration"
    )
    webhooks: list[WebhookConfig] | None = Field(
        default=None, description="Notification webhooks"
    )
    slack: SlackBotConfig | None = Field(
        default=None, description="Integrated Slack bot configuration"
    )
    mcp: McpConfig | None = Field(default=None, description="MCP server configuration")
    dashboard: DashboardConfig | None = Field(
        default=None, description="Built-in web dashboard configuration"
    )
    repos: dict[str, RepoConfig] = Field(
        default_factory=dict, description="Git repositories"
    )
    services: dict[str, ServiceConfig] = Field(
        default_factory=dict, description="Services to manage"
    )
    install: InstallConfig | None = Field(
        default=None, description="Install phase configuration"
    )
    self_update: SelfUpdateConfig | None = Field(
        default=None, alias="self", description="Self-update configuration"
    )
    orchestrator_client: OrchestratorClientConfig | None = Field(
        default=None, description="Orchestrator client configuration"
    )


def load_config(path: Path) -> HanielConfig:
    """Load and validate a haniel.yaml configuration file.

    Args:
        path: Path to the configuration file

    Returns:
        Validated HanielConfig instance

    Raises:
        FileNotFoundError: If the config file doesn't exist
        yaml.YAMLError: If the YAML is invalid
        pydantic.ValidationError: If the config doesn't match the schema
    """
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if data is None:
        data = {}

    return HanielConfig.model_validate(data)
