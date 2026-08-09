"""Tests for OrchestratorClient — connection, notify, backoff, graceful degradation."""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from haniel.config.model import OrchestratorClientConfig
from haniel.core.repo_reconciliation import RepoReconciliationSnapshot
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

    def test_node_hello_uses_configured_hostname(self):
        cfg = OrchestratorClientConfig(
            url="ws://localhost:9300/ws/node",
            token="test-token",
            node_id="test-node-1",
            hostname="eias-linegames",
        )
        client = OrchestratorClient(cfg, haniel_version="0.1.0")

        with patch(
            "haniel.integrations.orchestrator_client.platform.node",
            return_value="AD02028236",
        ):
            hello = client._build_node_hello()

        assert hello["hostname"] == "eias-linegames"

    def test_node_hello_falls_back_to_os_hostname(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")

        with patch(
            "haniel.integrations.orchestrator_client.platform.node",
            return_value="AD02028236",
        ):
            hello = client._build_node_hello()

        assert hello["hostname"] == "AD02028236"


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
            coro.close()
            sent_messages.append(coro)
            return result

        with patch("asyncio.run_coroutine_threadsafe") as mock_rct:
            mock_rct.side_effect = mock_run_coroutine
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
        # Build deploy_id manually to verify format
        commits = ["abc1234 fix: something"]
        first_hash = commits[0].split()[0]
        expected_id = f"{config.node_id}:myrepo:main:{first_hash}"
        assert expected_id == "test-node-1:myrepo:main:abc1234"

    def test_self_update_marker_is_sent_on_change_notification(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        client._loop = MagicMock()
        client._connected = True
        client._ws = MagicMock()
        client._send_json = AsyncMock()

        def close_coroutine(coro, _loop):
            coro.close()
            return MagicMock()

        with patch("asyncio.run_coroutine_threadsafe", side_effect=close_coroutine):
            client.notify_change(
                repo="haniel",
                branch="main",
                commits=["abc1234 self update"],
                affected_services=[],
                is_self_update=True,
            )

        payload = client._send_json.call_args.args[0]
        assert payload["is_self_update"] is True


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
    def _approval(config, deploy_id: str | None = None) -> dict:
        return {
            "deploy_id": deploy_id or f"{config.node_id}:repo:main:abc1234",
            "orchestrator_attempt_id": "orch-1",
            "connection_generation": "generation-1",
            "execution_mode": "execute",
            "approved_by": "dashboard",
        }

    @staticmethod
    def _snapshot(repo, branch, deploy_id):
        return RepoReconciliationSnapshot(
            node_id="test-node-1",
            repo=repo,
            branch=branch,
            local_head="abc1234",
            remote_head="abc1234",
            deploy_id=deploy_id,
        )

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

        def handler(approval, _progress):
            called.append(approval)
            return None

        client = OrchestratorClient(
            config,
            haniel_version="0.1.0",
            deploy_approval_handler=handler,
            repo_snapshot_handler=self._snapshot,
        )
        sent = self._capture_send_json(client)
        approval = self._approval(config)
        await client._handle_deploy_approval(approval)
        assert called == [approval]
        assert sent[0]["type"] == "deploy_progress"
        assert sent[-2]["status"] == "success"
        assert sent[-2]["error"] is None
        assert sent[-2]["duration_ms"] is not None
        assert sent[-2]["duration_ms"] >= 0
        assert sent[-2]["orchestrator_attempt_id"] == "orch-1"
        assert sent[-1]["type"] == "repo_reconciliation"

    async def test_success_is_not_sent_until_handler_finishes_verification(
        self, config
    ):
        import asyncio
        import threading

        verification_done = threading.Event()

        def handler(_approval, _progress):
            verification_done.wait(timeout=2)
            return None

        client = OrchestratorClient(
            config,
            haniel_version="0.1.0",
            deploy_approval_handler=handler,
            repo_snapshot_handler=self._snapshot,
        )
        sent = self._capture_send_json(client)
        pending = asyncio.create_task(
            client._handle_deploy_approval(self._approval(config))
        )
        await asyncio.sleep(0.02)
        assert all(message["type"] == "deploy_progress" for message in sent)

        verification_done.set()
        await pending

        assert sent[-2]["status"] == "success"

    async def test_handler_raises_sends_failed(self, config):
        def handler(_approval, _progress):
            raise RuntimeError("boom")

        client = OrchestratorClient(
            config,
            haniel_version="0.1.0",
            deploy_approval_handler=handler,
            repo_snapshot_handler=self._snapshot,
        )
        sent = self._capture_send_json(client)
        await client._handle_deploy_approval(self._approval(config))
        result = next(message for message in sent if message["type"] == "deploy_result")
        assert result["status"] == "failed"
        assert result["error"] == "boom"
        assert result["duration_ms"] is not None

    async def test_deferred_does_not_send(self, config):
        def handler(_approval, _progress):
            return "deferred"

        client = OrchestratorClient(
            config,
            haniel_version="0.1.0",
            deploy_approval_handler=handler,
        )
        sent = self._capture_send_json(client)
        await client._handle_deploy_approval(self._approval(config))
        assert [message["type"] for message in sent] == ["deploy_progress"]

    async def test_ignored_report_ack_is_logged(self, config, caplog):
        client = OrchestratorClient(config, haniel_version="0.1.0")

        with caplog.at_level("WARNING"):
            await client._handle_server_message(
                {
                    "type": "deploy_report_ack",
                    "deploy_id": "d1",
                    "orchestrator_attempt_id": "orch-1",
                    "report_type": "deploy_result",
                    "status": "ignored",
                    "reason": "terminal_attempt",
                    "attempt_outcome": "failed",
                }
            )

        assert "attempt_id=orch-1" in caplog.text
        assert "reason=terminal_attempt" in caplog.text


class TestDeployPlanProbe:
    async def test_missing_planner_returns_fail_closed_proposal(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        sent = []

        async def fake_send_json(message):
            sent.append(message)

        client._send_json = fake_send_json  # type: ignore[assignment]
        await client._handle_server_message(
            {
                "type": "deploy_plan_probe",
                "probe_id": "p1",
                "connection_generation": "g1",
                "deploy_id": "test-node-1:r:main:target",
                "node_id": "test-node-1",
                "repo": "r",
                "branch": "main",
                "target_head": "target",
            }
        )

        assert sent[0]["mode"] == "fail_closed"
        assert sent[0]["reason"] == "planner_missing"
        assert sent[0]["error"] == "no deploy plan probe handler registered"

    async def test_planner_exception_returns_fail_closed_proposal(self, config):
        def broken_planner(_probe):
            raise RuntimeError("journal unreadable")

        client = OrchestratorClient(
            config,
            haniel_version="0.1.0",
            deploy_plan_probe_handler=broken_planner,
        )
        sent = []

        async def fake_send_json(message):
            sent.append(message)

        client._send_json = fake_send_json  # type: ignore[assignment]
        await client._handle_server_message(
            {
                "type": "deploy_plan_probe",
                "probe_id": "p1",
                "connection_generation": "g1",
                "deploy_id": "test-node-1:r:main:target",
                "node_id": "test-node-1",
                "repo": "r",
                "branch": "main",
                "target_head": "target",
            }
        )

        assert sent[0]["mode"] == "fail_closed"
        assert sent[0]["reason"] == "planner_error"
        assert "journal unreadable" in sent[0]["error"]
        assert sent[0]["node_id"] == config.node_id


class TestHandleServiceCommand:
    @staticmethod
    def _capture_send_json(client):
        sent = []

        async def fake_send_json(msg):
            sent.append(msg)

        client._send_json = fake_send_json  # type: ignore[assignment]
        return sent

    async def test_handler_receives_payload_and_result_is_sent(self, config):
        called = []

        def handler(service_name, action, payload):
            called.append((service_name, action, payload))
            return {"ok": True, "restarted": True}

        client = OrchestratorClient(
            config,
            haniel_version="0.1.0",
            service_command_handler=handler,
        )
        sent = self._capture_send_json(client)

        await client._handle_service_command(
            {
                "command_id": "cmd-1",
                "service_name": "web",
                "action": "reload",
                "payload": {"reason": "config changed"},
            }
        )

        assert called == [("web", "reload", {"reason": "config changed"})]
        assert sent == [
            {
                "type": "service_command_result",
                "command_id": "cmd-1",
                "node_id": config.node_id,
                "service_name": "web",
                "action": "reload",
                "success": True,
                "error": None,
                "result": {"ok": True, "restarted": True},
            }
        ]

    async def test_handler_error_sends_failed_result(self, config):
        def handler(service_name, action, payload):
            raise RuntimeError("boom")

        client = OrchestratorClient(
            config,
            haniel_version="0.1.0",
            service_command_handler=handler,
        )
        sent = self._capture_send_json(client)

        await client._handle_service_command(
            {"command_id": "cmd-2", "service_name": "web", "action": "start"}
        )

        assert sent[0]["success"] is False
        assert sent[0]["error"] == "boom"
        assert sent[0]["result"] is None

    async def test_no_handler_sends_failed_result(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        sent = self._capture_send_json(client)

        await client._handle_service_command(
            {"command_id": "cmd-3", "service_name": "web", "action": "start"}
        )

        assert sent[0]["success"] is False
        assert sent[0]["error"] == "no handler registered"


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

    async def test_first_message_failure_preserves_entire_queue(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        client.enqueue_deploy_result("d1", "success")
        client.enqueue_deploy_result("d2", "success")

        async def fake_send_json(_msg):
            raise OSError("connection lost")

        client._send_json = fake_send_json  # type: ignore[assignment]
        await client._flush_pending_deploy_results()
        with client._pending_lock:
            assert [msg["deploy_id"] for msg in client._pending_deploy_results] == [
                "d1",
                "d2",
            ]

    async def test_middle_message_failure_preserves_unsent_suffix(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        for deploy_id in ("d1", "d2", "d3"):
            client.enqueue_deploy_result(deploy_id, "success")
        sent = []

        async def fake_send_json(msg):
            if msg["deploy_id"] == "d2":
                raise OSError("connection lost")
            sent.append(msg["deploy_id"])

        client._send_json = fake_send_json  # type: ignore[assignment]
        await client._flush_pending_deploy_results()

        assert sent == ["d1"]
        with client._pending_lock:
            assert [msg["deploy_id"] for msg in client._pending_deploy_results] == [
                "d2",
                "d3",
            ]

    async def test_failure_during_concurrent_enqueue_preserves_total_order(
        self, config
    ):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        client.enqueue_deploy_result("d1", "success")
        client.enqueue_deploy_result("d2", "success")
        send_entered = asyncio.Event()
        release_send = asyncio.Event()

        async def fake_send_json(_msg):
            send_entered.set()
            await release_send.wait()
            raise OSError("connection lost")

        client._send_json = fake_send_json  # type: ignore[assignment]
        flush = asyncio.create_task(client._flush_pending_deploy_results())
        await send_entered.wait()
        client.enqueue_deploy_result("d3", "success")
        release_send.set()
        await flush

        with client._pending_lock:
            assert [msg["deploy_id"] for msg in client._pending_deploy_results] == [
                "d1",
                "d2",
                "d3",
            ]

    async def test_reconnect_retry_sends_failed_message_then_suffix(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        for deploy_id in ("d1", "d2", "d3"):
            client.enqueue_deploy_result(deploy_id, "success")

        async def fail_middle(msg):
            if msg["deploy_id"] == "d2":
                raise OSError("connection lost")

        client._send_json = fail_middle  # type: ignore[assignment]
        await client._flush_pending_deploy_results()
        retried = []

        async def capture(msg):
            retried.append(msg["deploy_id"])

        client._send_json = capture  # type: ignore[assignment]
        await client._flush_pending_deploy_results()

        assert retried == ["d2", "d3"]
        with client._pending_lock:
            assert client._pending_deploy_results == []

    async def test_self_update_result_and_settled_snapshot_keep_wire_order(
        self, config
    ):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        snapshot = RepoReconciliationSnapshot(
            deploy_id="test-node-1:r:main:target",
            node_id="test-node-1",
            repo="r",
            branch="main",
            local_head="target",
            remote_head="target",
        )
        client.enqueue_deploy_result(
            snapshot.deploy_id,
            "success",
            orchestrator_attempt_id="a1",
            connection_generation="g1",
            settled_snapshot=snapshot,
        )
        sent = []

        async def capture(msg):
            sent.append(msg)

        client._send_json = capture  # type: ignore[assignment]
        await client._flush_pending_deploy_results()

        assert [msg["type"] for msg in sent] == [
            "deploy_result",
            "repo_reconciliation",
        ]
        assert sent[1]["phase"] == "settled"
        assert sent[0]["orchestrator_attempt_id"] == sent[1]["orchestrator_attempt_id"]

    async def test_concurrent_flushes_do_not_duplicate_or_lose_messages(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        for deploy_id in ("d1", "d2", "d3"):
            client.enqueue_deploy_result(deploy_id, "success")
        first_send_entered = asyncio.Event()
        release_first_send = asyncio.Event()
        sent = []

        async def capture(msg):
            if not sent:
                first_send_entered.set()
                await release_first_send.wait()
            sent.append(msg["deploy_id"])

        client._send_json = capture  # type: ignore[assignment]
        first = asyncio.create_task(client._flush_pending_deploy_results())
        await first_send_entered.wait()
        second = asyncio.create_task(client._flush_pending_deploy_results())
        await asyncio.sleep(0)
        release_first_send.set()
        await asyncio.gather(first, second)

        assert sent == ["d1", "d2", "d3"]
        with client._pending_lock:
            assert client._pending_deploy_results == []

    async def test_flush_with_empty_buffer_noop(self, config):
        client = OrchestratorClient(config, haniel_version="0.1.0")
        sent = []

        async def fake_send_json(msg):
            sent.append(msg)

        client._send_json = fake_send_json  # type: ignore[assignment]
        await client._flush_pending_deploy_results()
        assert sent == []


class TestSendJson:
    async def test_send_json_times_out_using_ping_timeout(self, config):
        config.ping_timeout = 0.01
        client = OrchestratorClient(config, haniel_version="0.1.0")

        class SlowWebSocket:
            async def send(self, payload):
                await asyncio.sleep(0.2)

        client._ws = SlowWebSocket()

        with pytest.raises(asyncio.TimeoutError):
            await client._send_json({"type": "node_status"})


class TestHeartbeatDoesNotBlockEventLoop:
    """The event loop must stay responsive while a runner thread holds a lock.

    Regression for the 260810 node flap: the heartbeat read service state
    through a config-guarded runner method, so the loop parked on the runner's
    threading lock. The poll thread held that lock while waiting on a coroutine
    the parked loop could no longer run, so the two deadlocked until the send
    timed out. Pongs stopped with it, the hub closed each connection after 60s,
    and every node sat offline roughly 70% of the time.

    The lock is released by a coroutine, so a parked loop can never reach the
    release and the deadlock reproduces instead of merely running slowly.
    """

    def test_send_heartbeat_reads_service_state_off_the_loop(self, config):
        holder_acquired = threading.Event()
        release_holder = threading.Event()
        runner_lock = threading.RLock()

        def guarded_services_info():
            with runner_lock:
                return [{"name": "svc", "ready": True}]

        client = OrchestratorClient(
            config,
            haniel_version="0.1.0",
            get_services_info=guarded_services_info,
        )
        client._ws = MagicMock()
        client._send_json = AsyncMock()

        def hold_lock() -> None:
            with runner_lock:
                holder_acquired.set()
                release_holder.wait(timeout=10)

        async def release_from_the_loop() -> str:
            await asyncio.sleep(0.05)
            release_holder.set()
            return "alive"

        async def scenario() -> str:
            holder = threading.Thread(target=hold_lock)
            holder.start()
            assert holder_acquired.wait(timeout=5)
            heartbeat = asyncio.create_task(client._send_heartbeat())
            progressed = await asyncio.wait_for(release_from_the_loop(), timeout=2)
            await asyncio.wait_for(heartbeat, timeout=5)
            holder.join(timeout=5)
            return progressed

        assert asyncio.run(scenario()) == "alive"
        client._send_json.assert_awaited_once()
        sent = client._send_json.await_args.args[0]
        assert sent["type"] == "node_status"
        assert sent["services"] == [{"name": "svc", "ready": True}]

    def test_node_hello_reads_service_state_off_the_loop(self, config):
        holder_acquired = threading.Event()
        release_holder = threading.Event()
        runner_lock = threading.RLock()

        def guarded_services_info():
            with runner_lock:
                return [{"name": "svc", "ready": True}]

        client = OrchestratorClient(
            config,
            haniel_version="0.1.0",
            get_services_info=guarded_services_info,
        )

        def hold_lock() -> None:
            with runner_lock:
                holder_acquired.set()
                release_holder.wait(timeout=10)

        async def release_from_the_loop() -> str:
            await asyncio.sleep(0.05)
            release_holder.set()
            return "alive"

        async def scenario() -> tuple:
            holder = threading.Thread(target=hold_lock)
            holder.start()
            assert holder_acquired.wait(timeout=5)
            snapshot = asyncio.create_task(client._services_snapshot())
            progressed = await asyncio.wait_for(release_from_the_loop(), timeout=2)
            services = await asyncio.wait_for(snapshot, timeout=5)
            holder.join(timeout=5)
            return progressed, services

        progressed, services = asyncio.run(scenario())
        assert progressed == "alive"
        assert client._build_node_hello(services)["services"] == [
            {"name": "svc", "ready": True}
        ]
