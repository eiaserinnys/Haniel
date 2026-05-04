"""Tests for NodeRegistry — register, unregister, heartbeat, stale detection."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from haniel_orch.event_store import EventStore
from haniel_orch.node_registry import ConnectedNode, NodeRegistry
from haniel_orch.protocol import DeployStatus, NodeHello


def _make_hello(node_id: str = "n1") -> NodeHello:
    return NodeHello(
        node_id=node_id,
        token="secret",
        hostname=f"host-{node_id}",
        os="Linux",
        arch="x86_64",
        haniel_version="0.14.2",
    )


class TestRegister:
    async def test_registers_node_in_memory(self, store: EventStore):
        registry = NodeRegistry(store)
        ws = MagicMock()
        hello = _make_hello("n1")

        await registry.register(ws, hello)

        node = registry.get_node("n1")
        assert node is not None
        assert node.node_id == "n1"
        assert node.websocket is ws
        assert node.hello is hello

    async def test_registers_node_in_db(self, store: EventStore):
        registry = NodeRegistry(store)
        ws = MagicMock()
        hello = _make_hello("n1")

        await registry.register(ws, hello)

        nodes = await store.get_nodes()
        assert len(nodes) == 1
        assert nodes[0]["node_id"] == "n1"
        assert nodes[0]["hostname"] == "host-n1"
        assert nodes[0]["connected"] == 1

    async def test_replaces_existing_node(self, store: EventStore):
        registry = NodeRegistry(store)
        ws1 = MagicMock()
        ws2 = MagicMock()
        hello = _make_hello("n1")

        await registry.register(ws1, hello)
        await registry.register(ws2, hello)

        node = registry.get_node("n1")
        assert node.websocket is ws2


class TestUnregister:
    async def test_removes_from_memory(self, store: EventStore):
        registry = NodeRegistry(store)
        ws = MagicMock()
        await registry.register(ws, _make_hello("n1"))

        await registry.unregister("n1")

        assert registry.get_node("n1") is None

    async def test_marks_disconnected_in_db(self, store: EventStore):
        registry = NodeRegistry(store)
        ws = MagicMock()
        await registry.register(ws, _make_hello("n1"))

        await registry.unregister("n1")

        nodes = await store.get_nodes()
        assert nodes[0]["connected"] == 0

    async def test_fails_deploying_events(self, store: EventStore):
        registry = NodeRegistry(store)
        ws = MagicMock()
        await registry.register(ws, _make_hello("n1"))

        # Create a deploying event
        await store.create_deploy_event(
            deploy_id="d1",
            node_id="n1",
            repo="r",
            branch="main",
            commits=["h msg"],
            affected_services=[],
            diff_stat=None,
            detected_at="2026-01-01T00:00:00Z",
        )
        await store.update_deploy_status("d1", DeployStatus.DEPLOYING)

        await registry.unregister("n1")

        event = await store.get_deploy_event("d1")
        assert event["status"] == "failed"
        assert event["error"] == "node disconnected"

    async def test_unregister_nonexistent_does_not_raise(self, store: EventStore):
        registry = NodeRegistry(store)
        # Should not raise
        await registry.unregister("nonexistent")


class TestHeartbeat:
    async def test_updates_last_heartbeat(self, store: EventStore):
        registry = NodeRegistry(store)
        ws = MagicMock()
        await registry.register(ws, _make_hello("n1"))

        node = registry.get_node("n1")
        old_heartbeat = node.last_heartbeat

        # Simulate time passing
        node.last_heartbeat -= 10

        await registry.heartbeat("n1")
        assert node.last_heartbeat > old_heartbeat - 10

    async def test_heartbeat_nonexistent_does_not_raise(self, store: EventStore):
        registry = NodeRegistry(store)
        # Should not raise
        await registry.heartbeat("nonexistent")


class TestGetConnectedNodes:
    async def test_returns_all_connected(self, store: EventStore):
        registry = NodeRegistry(store)
        await registry.register(MagicMock(), _make_hello("n1"))
        await registry.register(MagicMock(), _make_hello("n2"))

        nodes = registry.get_connected_nodes()
        assert len(nodes) == 2
        ids = {n.node_id for n in nodes}
        assert ids == {"n1", "n2"}

    async def test_empty_when_none(self, store: EventStore):
        registry = NodeRegistry(store)
        assert registry.get_connected_nodes() == []


class TestCheckStale:
    async def test_identifies_stale_nodes(self, store: EventStore):
        registry = NodeRegistry(store, heartbeat_timeout=5.0)
        ws = MagicMock()
        await registry.register(ws, _make_hello("n1"))

        # Force heartbeat to be old
        node = registry.get_node("n1")
        node.last_heartbeat = time.time() - 10

        stale = await registry.check_stale()
        assert stale == ["n1"]
        assert registry.get_node("n1") is None

    async def test_keeps_fresh_nodes(self, store: EventStore):
        registry = NodeRegistry(store, heartbeat_timeout=90.0)
        ws = MagicMock()
        await registry.register(ws, _make_hello("n1"))

        stale = await registry.check_stale()
        assert stale == []
        assert registry.get_node("n1") is not None

    async def test_mixed_stale_and_fresh(self, store: EventStore):
        registry = NodeRegistry(store, heartbeat_timeout=5.0)
        await registry.register(MagicMock(), _make_hello("n1"))
        await registry.register(MagicMock(), _make_hello("n2"))

        # Make n1 stale, keep n2 fresh
        registry.get_node("n1").last_heartbeat = time.time() - 10

        stale = await registry.check_stale()
        assert stale == ["n1"]
        assert registry.get_node("n1") is None
        assert registry.get_node("n2") is not None
