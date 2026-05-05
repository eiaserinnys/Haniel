"""Tests for service command — API endpoint, hub broadcast, protocol."""

import json
from unittest.mock import AsyncMock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from haniel_orch.api import create_api_routes
from haniel_orch.event_store import EventStore
from haniel_orch.hub import WebSocketHub
from haniel_orch.node_registry import NodeRegistry
from haniel_orch.protocol import (
    NodeHello,
    ServiceCommand,
    ServiceCommandResult,
    parse_node_message,
)


@pytest.fixture
async def registry(store: EventStore):
    return NodeRegistry(store)


@pytest.fixture
async def hub(registry: NodeRegistry, store: EventStore):
    return WebSocketHub(registry, store, token="test-token")


@pytest.fixture
def app(hub: WebSocketHub, store: EventStore):
    routes = create_api_routes(hub, store)
    return Starlette(routes=routes)


class TestServiceCommandEndpoint:
    """POST /api/orch/service-command tests."""

    async def test_sends_command_to_connected_node(
        self, app, hub, registry, store
    ):
        # Register a node
        ws = AsyncMock()
        hello = NodeHello(
            node_id="n1",
            token="t",
            hostname="h",
            os="Linux",
            arch="x86_64",
            haniel_version="0.1.0",
        )
        await registry.register(ws, hello)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/orch/service-command",
            json={"node_id": "n1", "service_name": "bot", "action": "restart"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "sent"
        assert "command_id" in data
        assert "n1:bot:restart:" in data["command_id"]

        # Verify message was sent to node
        ws.send_text.assert_called_once()
        sent_msg = json.loads(ws.send_text.call_args[0][0])
        assert sent_msg["type"] == "service_command"
        assert sent_msg["service_name"] == "bot"
        assert sent_msg["action"] == "restart"

    async def test_returns_503_for_disconnected_node(self, app):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/orch/service-command",
            json={"node_id": "offline-node", "service_name": "bot", "action": "restart"},
        )
        assert resp.status_code == 503
        assert "not connected" in resp.json()["error"]

    async def test_returns_400_for_missing_fields(self, app):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/orch/service-command",
            json={"node_id": "n1"},  # missing service_name, action
        )
        assert resp.status_code == 400

    async def test_returns_400_for_invalid_action(self, app):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/orch/service-command",
            json={"node_id": "n1", "service_name": "bot", "action": "delete"},
        )
        assert resp.status_code == 400
        assert "restart" in resp.json()["error"]

    async def test_stop_action(self, app, hub, registry, store):
        ws = AsyncMock()
        hello = NodeHello(
            node_id="n1",
            token="t",
            hostname="h",
            os="Linux",
            arch="x86_64",
            haniel_version="0.1.0",
        )
        await registry.register(ws, hello)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/orch/service-command",
            json={"node_id": "n1", "service_name": "svc1", "action": "stop"},
        )

        assert resp.status_code == 200
        sent_msg = json.loads(ws.send_text.call_args[0][0])
        assert sent_msg["action"] == "stop"


class TestServiceCommandResultBroadcast:
    """Hub broadcasts ServiceCommandResult to dashboards."""

    async def test_broadcasts_result(self, hub: WebSocketHub):
        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}

        result = ServiceCommandResult(
            command_id="n1:bot:restart:123",
            node_id="n1",
            service_name="bot",
            action="restart",
            success=True,
        )
        await hub._handle_service_command_result(result)

        ws_dash.send_text.assert_called_once()
        data = json.loads(ws_dash.send_text.call_args[0][0])
        assert data["type"] == "service_command_result"
        assert data["success"] is True
        assert data["service_name"] == "bot"
        assert data["node_id"] == "n1"

    async def test_broadcasts_failure(self, hub: WebSocketHub):
        ws_dash = AsyncMock()
        hub._dashboard_connections = {ws_dash}

        result = ServiceCommandResult(
            command_id="n1:svc:stop:456",
            node_id="n1",
            service_name="svc",
            action="stop",
            success=False,
            error="Unknown service: svc",
        )
        await hub._handle_service_command_result(result)

        data = json.loads(ws_dash.send_text.call_args[0][0])
        assert data["success"] is False
        assert data["error"] == "Unknown service: svc"


class TestServiceCommandProtocol:
    """Protocol model and parsing tests."""

    def test_service_command_model(self):
        cmd = ServiceCommand(
            command_id="n1:bot:restart:123",
            service_name="bot",
            action="restart",
        )
        data = json.loads(cmd.model_dump_json())
        assert data["type"] == "service_command"
        assert data["service_name"] == "bot"

    def test_service_command_result_model(self):
        result = ServiceCommandResult(
            command_id="n1:bot:restart:123",
            node_id="n1",
            service_name="bot",
            action="restart",
            success=True,
        )
        data = json.loads(result.model_dump_json())
        assert data["type"] == "service_command_result"
        assert data["success"] is True

    def test_parse_service_command_result(self):
        raw = json.dumps({
            "type": "service_command_result",
            "command_id": "n1:bot:restart:123",
            "node_id": "n1",
            "service_name": "bot",
            "action": "restart",
            "success": False,
            "error": "service crashed",
        })
        msg = parse_node_message(raw)
        assert isinstance(msg, ServiceCommandResult)
        assert msg.success is False
        assert msg.error == "service crashed"
