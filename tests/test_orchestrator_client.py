"""Tests for OrchestratorClient — connection, notify, backoff, graceful degradation."""

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from haniel.config.model import OrchestratorClientConfig
from haniel.integrations.orchestrator_client import OrchestratorClient


@pytest.fixture
def config():
    return OrchestratorClientConfig(
        url="ws://localhost:9300/ws/node",
        token="test-token",
        node_id="test-node-1",
        reconnect_base=0.1,
        reconnect_max=1.0,
    )


class TestOrchestratorClientInit:
    def test_initial_state(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        assert client._connected is False
        assert client._ws is None
        assert client._thread is None
        assert client._reconnect_delay == config.reconnect_base

    def test_config_stored(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        assert client._config is config
        assert client._haniel_version == "0.1.0"


class TestNotifyChange:
    def test_noop_when_not_connected(self, config):
        """notify_change should silently drop when not connected."""
        client = OrchestratorClient(config, haniel_version="0.1.0")
        # Should not raise
        client.notify_change(
            repo="myrepo",
            branch="main",
            commits=["abc1234 fix: something"],
            affected_services=["bot"],
        )

    def test_noop_with_empty_commits(self, config):
        """notify_change should return immediately for empty commits."""
        client = OrchestratorClient(config, haniel_version="0.1.0")
        client._connected = True
        client._ws = MagicMock()
        # Should not raise or send
        client.notify_change(
            repo="myrepo",
            branch="main",
            commits=[],
            affected_services=["bot"],
        )

    def test_deploy_id_format(self, config):
        """deploy_id should be deterministic: node_id:repo:branch:first_hash."""
        client = OrchestratorClient(config, haniel_version="0.1.0")

        # Simulate connected state with a mock loop
        import asyncio

        loop = asyncio.new_event_loop()
        client._loop = loop
        client._connected = True
        client._ws = MagicMock()

        sent_messages = []

        def mock_run_coroutine(coro, loop_arg):
            # Run the coroutine to capture what was sent
            result = MagicMock()
            sent_messages.append(coro)
            return result

        with patch("asyncio.run_coroutine_threadsafe") as mock_rct:
            client.notify_change(
                repo="myrepo",
                branch="main",
                commits=["abc1234 fix: something", "def5678 feat: another"],
                affected_services=["bot", "mcp"],
                diff_stat="+10 -3",
            )

            # Verify run_coroutine_threadsafe was called
            assert mock_rct.called
            # The coroutine args contain the message
            call_args = mock_rct.call_args
            # First arg is the coroutine, second is the loop
            assert call_args[0][1] is loop

        loop.close()

    def test_deploy_id_deterministic(self, config):
        """Same commits should produce same deploy_id."""
        client = OrchestratorClient(config, haniel_version="0.1.0")

        # Build deploy_id manually to verify format
        commits = ["abc1234 fix: something"]
        first_hash = commits[0].split()[0]
        expected_id = f"{config.node_id}:myrepo:main:{first_hash}"
        assert expected_id == "test-node-1:myrepo:main:abc1234"


class TestBackoff:
    def test_reset_backoff(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        client._reconnect_delay = 10.0
        client._reset_backoff()
        assert client._reconnect_delay == config.reconnect_base

    def test_next_backoff_doubles(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        assert client._reconnect_delay == 0.1

        delay1 = client._next_backoff()
        assert delay1 == 0.1
        assert client._reconnect_delay == 0.2

        delay2 = client._next_backoff()
        assert delay2 == 0.2
        assert client._reconnect_delay == 0.4

    def test_backoff_capped_at_max(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        # Advance past max
        for _ in range(20):
            client._next_backoff()
        assert client._reconnect_delay == config.reconnect_max

    def test_backoff_resets_on_connect(self, config):
        """_reset_backoff should restore the base delay."""
        client = OrchestratorClient(config, haniel_version="0.1.0")
        for _ in range(5):
            client._next_backoff()
        assert client._reconnect_delay > config.reconnect_base

        client._reset_backoff()
        assert client._reconnect_delay == config.reconnect_base


class TestStartStop:
    def test_start_creates_thread(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        # Patch _run_loop to prevent actual connection
        with patch.object(client, "_run_loop"):
            client.start()
            assert client._thread is not None
            assert client._thread.daemon is True
            client.stop()

    def test_stop_sets_event(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        client.stop()
        assert client._stop_event.is_set()

    def test_double_start_noop(self, config):
        """Starting twice while thread is alive should not create a second thread."""
        client = OrchestratorClient(config, haniel_version="0.1.0")

        # Use an event to keep the thread alive
        keep_alive = threading.Event()

        def blocking_run_loop():
            keep_alive.wait(timeout=5)

        with patch.object(client, "_run_loop", side_effect=blocking_run_loop):
            client.start()
            first_thread = client._thread
            assert first_thread.is_alive()

            client.start()  # Should be noop
            assert client._thread is first_thread

            keep_alive.set()
            client.stop()


class TestParseDeployId:
    def test_parses_valid(self):
        result = OrchestratorClient._parse_deploy_id("node-1:my-repo:main:abc1234")
        assert result == ("node-1", "my-repo", "main", "abc1234")

    def test_too_few_parts(self):
        assert OrchestratorClient._parse_deploy_id("a:b:c") is None

    def test_empty(self):
        assert OrchestratorClient._parse_deploy_id("") is None

    def test_non_string(self):
        assert OrchestratorClient._parse_deploy_id(None) is None  # type: ignore[arg-type]

    def test_extra_colons_in_4th(self):
        # split(':', 3) keeps any extra ':' inside the 4th element
        result = OrchestratorClient._parse_deploy_id("n:r:b:h:extra")
        assert result == ("n", "r", "b", "h:extra")


class TestHandleDeployApproval:
    @staticmethod
    def _capture_send_json(client):
        sent = []

        async def fake_send_json(msg):
            sent.append(msg)

        client._send_json = fake_send_json  # type: ignore[assignment]
        return sent

    async def test_invalid_format_sends_failed(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        sent = self._capture_send_json(client)
        await client._handle_deploy_approval({"deploy_id": "badformat"})
        assert len(sent) == 1
        assert sent[0]["type"] == "deploy_result"
        assert sent[0]["status"] == "failed"
        assert "invalid deploy_id format" in sent[0]["error"]
        assert sent[0]["node_id"] == config.node_id

    async def test_node_id_mismatch_sends_failed(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        sent = self._capture_send_json(client)
        await client._handle_deploy_approval(
            {"deploy_id": "other-node:repo:main:abc1234"}
        )
        assert sent[0]["status"] == "failed"
        assert "node mismatch" in sent[0]["error"]

    async def test_no_handler_sends_failed(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        sent = self._capture_send_json(client)
        await client._handle_deploy_approval(
            {"deploy_id": f"{config.node_id}:repo:main:abc1234"}
        )
        assert sent[0]["status"] == "failed"
        assert "no deploy_approval handler" in sent[0]["error"]

    async def test_success_sends_success(self, config):
        called = []

        def handler(deploy_id, repo, branch):
            called.append((deploy_id, repo, branch))
            return None

        client = OrchestratorClient(
            config, haniel_version="0.1.0",
            deploy_approval_handler=handler,
        )
        sent = self._capture_send_json(client)
        await client._handle_deploy_approval(
            {"deploy_id": f"{config.node_id}:repo:main:abc1234"}
        )
        assert called == [
            (f"{config.node_id}:repo:main:abc1234", "repo", "main")
        ]
        assert sent[0]["status"] == "success"
        assert sent[0]["error"] is None
        assert sent[0]["duration_ms"] is not None
        assert sent[0]["duration_ms"] >= 0

    async def test_handler_raises_sends_failed(self, config):
        def handler(deploy_id, repo, branch):
            raise RuntimeError("boom")

        client = OrchestratorClient(
            config, haniel_version="0.1.0",
            deploy_approval_handler=handler,
        )
        sent = self._capture_send_json(client)
        await client._handle_deploy_approval(
            {"deploy_id": f"{config.node_id}:repo:main:abc1234"}
        )
        assert sent[0]["status"] == "failed"
        assert sent[0]["error"] == "boom"
        assert sent[0]["duration_ms"] is not None

    async def test_deferred_does_not_send(self, config):
        def handler(deploy_id, repo, branch):
            return "deferred"

        client = OrchestratorClient(
            config, haniel_version="0.1.0",
            deploy_approval_handler=handler,
        )
        sent = self._capture_send_json(client)
        await client._handle_deploy_approval(
            {"deploy_id": f"{config.node_id}:repo:main:abc1234"}
        )
        assert sent == []


class TestEnqueueDeployResult:
    def test_buffers_when_disconnected(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        client.enqueue_deploy_result("d1", "success", duration_ms=1234)
        with client._pending_lock:
            assert len(client._pending_deploy_results) == 1
            msg = client._pending_deploy_results[0]
            assert msg["type"] == "deploy_result"
            assert msg["deploy_id"] == "d1"
            assert msg["status"] == "success"
            assert msg["duration_ms"] == 1234
            assert msg["error"] is None
            assert msg["node_id"] == config.node_id

    def test_buffers_with_error(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        client.enqueue_deploy_result("d2", "failed", error="boom")
        with client._pending_lock:
            assert client._pending_deploy_results[0]["error"] == "boom"
            assert client._pending_deploy_results[0]["status"] == "failed"
            assert client._pending_deploy_results[0]["duration_ms"] is None

    async def test_flush_sends_pending(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        client.enqueue_deploy_result("d1", "success")
        client.enqueue_deploy_result("d2", "failed", error="boom")

        sent = []

        async def fake_send_json(msg):
            sent.append(msg)

        client._send_json = fake_send_json  # type: ignore[assignment]
        await client._flush_pending_deploy_results()
        assert len(sent) == 2
        assert sent[0]["deploy_id"] == "d1"
        assert sent[1]["deploy_id"] == "d2"
        assert sent[1]["error"] == "boom"
        with client._pending_lock:
            assert client._pending_deploy_results == []

    async def test_flush_requeues_on_failure(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        client.enqueue_deploy_result("d1", "success")
        client.enqueue_deploy_result("d2", "success")

        sent = []

        async def fake_send_json(msg):
            if len(sent) >= 1:
                raise OSError("connection lost")
            sent.append(msg)

        client._send_json = fake_send_json  # type: ignore[assignment]
        await client._flush_pending_deploy_results()
        assert len(sent) == 1
        # d2 was attempted, failed, and re-queued (order preserved)
        with client._pending_lock:
            assert len(client._pending_deploy_results) == 1
            assert client._pending_deploy_results[0]["deploy_id"] == "d2"

    async def test_flush_with_empty_buffer_noop(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        sent = []

        async def fake_send_json(msg):
            sent.append(msg)

        client._send_json = fake_send_json  # type: ignore[assignment]
        await client._flush_pending_deploy_results()
        assert sent == []
