"""Dependency and low-level MCP wiring compatibility contracts."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Callable

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_dependencies_have_major_upper_bounds() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = set(project["project"]["dependencies"])

    assert {
        "pydantic>=2.0.0,<3",
        "starlette>=0.40.0,<2",
        "uvicorn[standard]>=0.30.0,<1",
        "claude-agent-sdk>=0.1.0,<1",
        "mcp>=1.0.0,<2",
        "slack-bolt>=1.20.0,<2",
    } <= dependencies


class _RecordingServer:
    def __init__(self, name: str) -> None:
        self.name = name
        self.registrations: list[str] = []

    def _factory(self, name: str) -> Callable[..., Callable[..., Any]]:
        self.registrations.append(name)

        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            return handler

        return decorator

    def list_resources(self) -> Callable[..., Callable[..., Any]]:
        return self._factory("list_resources")

    def read_resource(self) -> Callable[..., Callable[..., Any]]:
        return self._factory("read_resource")

    def list_tools(self) -> Callable[..., Callable[..., Any]]:
        return self._factory("list_tools")

    def call_tool(self) -> Callable[..., Callable[..., Any]]:
        return self._factory("call_tool")


def test_probe_registers_all_handlers_and_builds_session_manager() -> None:
    from haniel.integrations.mcp_compatibility import (
        validate_mcp_runtime_compatibility,
    )

    server = _RecordingServer("probe")
    manager_calls: list[dict[str, Any]] = []

    def manager_factory(**kwargs: Any) -> object:
        manager_calls.append(kwargs)
        return object()

    validate_mcp_runtime_compatibility(
        server_factory=lambda name: server,
        session_manager_factory=manager_factory,
    )

    assert server.registrations == [
        "list_resources",
        "read_resource",
        "list_tools",
        "call_tool",
    ]
    assert manager_calls == [{"app": server, "json_response": True, "stateless": False}]


def test_probe_rejects_removed_decorator_api() -> None:
    from haniel.integrations.mcp_compatibility import (
        validate_mcp_runtime_compatibility,
    )

    class BrokenServer:
        pass

    with pytest.raises(AttributeError, match="list_resources"):
        validate_mcp_runtime_compatibility(
            server_factory=lambda name: BrokenServer(),
            session_manager_factory=lambda **kwargs: object(),
        )


def test_installed_mcp_runtime_passes_compatibility_probe() -> None:
    from haniel.integrations.mcp_compatibility import (
        validate_mcp_runtime_compatibility,
    )

    validate_mcp_runtime_compatibility()
