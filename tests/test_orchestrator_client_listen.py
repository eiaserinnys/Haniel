"""The receive loop hands work off; execution and results outlive one socket."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable

import pytest

from haniel.config.model import OrchestratorClientConfig
from haniel.core.repo_reconciliation import RepoReconciliationSnapshot
from haniel.integrations.deploy_reporting import ApprovalRevalidationError
from haniel.integrations.orchestrator_client import OrchestratorClient

TARGET_A = "a" * 40
TARGET_B = "b" * 40


@pytest.fixture
def config() -> OrchestratorClientConfig:
    return OrchestratorClientConfig(
        url="ws://localhost:9300/ws/node",
        token="test-token",
        node_id="test-node-1",
        reconnect_base=0.01,
        reconnect_max=0.02,
        ping_timeout=1.0,
    )


def _approval(config: OrchestratorClientConfig, target: str, attempt: str) -> dict:
    return {
        "type": "deploy_approval",
        "deploy_id": f"{config.node_id}:repo:main:{target}",
        "orchestrator_attempt_id": attempt,
        "connection_generation": "generation-1",
        "execution_mode": "execute",
        "probe_id": None,
        "preflight_fingerprint": None,
        "approved_by": "dashboard",
    }


def _service_command(command_id: str) -> dict:
    return {
        "type": "service_command",
        "command_id": command_id,
        "service_name": "web",
        "action": "start",
        "payload": None,
    }


def _plan_probe(config: OrchestratorClientConfig, probe_id: str) -> dict:
    return {
        "type": "deploy_plan_probe",
        "probe_id": probe_id,
        "connection_generation": "generation-1",
        "deploy_id": f"{config.node_id}:repo:main:{TARGET_A}",
        "node_id": config.node_id,
        "repo": "repo",
        "branch": "main",
        "target_head": TARGET_A,
    }


def _snapshot(repo: str, branch: str, deploy_id: str) -> RepoReconciliationSnapshot:
    target = deploy_id.rsplit(":", 1)[-1]
    return RepoReconciliationSnapshot(
        node_id="test-node-1",
        repo=repo,
        branch=branch,
        local_head=target,
        remote_head=target,
        deploy_id=deploy_id,
    )


class HeldDeployment:
    """Approval handler that parks each deploy on a threading barrier."""

    def __init__(self) -> None:
        self.entered: dict[str, threading.Event] = {}
        self.release: dict[str, threading.Event] = {}
        self.order: list[str] = []
        self._lock = threading.Lock()

    def _events(self, deploy_id: str) -> tuple[threading.Event, threading.Event]:
        with self._lock:
            entered = self.entered.setdefault(deploy_id, threading.Event())
            release = self.release.setdefault(deploy_id, threading.Event())
        return entered, release

    def handle(self, approval: dict, _progress: Callable[[str], None]) -> None:
        deploy_id = approval["deploy_id"]
        entered, release = self._events(deploy_id)
        self.order.append(deploy_id)
        entered.set()
        if not release.wait(timeout=10):
            raise TimeoutError(f"deployment {deploy_id} was never released")
        return None

    async def wait_entered(self, deploy_id: str) -> None:
        entered, _release = self._events(deploy_id)
        assert await asyncio.to_thread(entered.wait, 5), f"{deploy_id} never started"

    def let_go(self, deploy_id: str) -> None:
        _entered, release = self._events(deploy_id)
        release.set()


class SentLog:
    """Capture outbound messages and let tests await the next matching one."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self._changed = asyncio.Event()

    async def send(self, message: dict) -> None:
        self.messages.append(message)
        self._changed.set()

    def of_type(self, message_type: str) -> list[dict]:
        return [m for m in self.messages if m["type"] == message_type]

    async def wait_for(
        self, predicate: Callable[[dict], bool], timeout: float = 5.0
    ) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for message in self.messages:
                if predicate(message):
                    return message
            remaining = deadline - loop.time()
            assert remaining > 0, f"no matching message in {self.messages!r}"
            self._changed.clear()
            await asyncio.wait_for(self._changed.wait(), remaining)


class FakeWebSocket:
    """Minimal websockets-like socket: async iteration plus send."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent = SentLog()
        self.opened = asyncio.Event()
        self.closed = False

    def __aiter__(self) -> FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def send(self, payload: str) -> None:
        if self.closed:
            raise ConnectionError("fake socket is closed")
        await self.sent.send(json.loads(payload))

    def deliver(self, message: dict) -> None:
        self.incoming.put_nowait(json.dumps(message))

    def drop(self) -> None:
        self.closed = True
        self.incoming.put_nowait(None)


class FakeConnect:
    """Stand-in for websockets.connect that yields prepared sockets in order."""

    def __init__(self, sockets: list[FakeWebSocket]) -> None:
        self._sockets = list(sockets)
        self.allow: list[asyncio.Event] = [asyncio.Event() for _ in sockets]
        for gate in self.allow:
            gate.set()
        self._index = 0

    def __call__(self, _url: str, **_kwargs) -> FakeConnect:
        return self

    async def __aenter__(self) -> FakeWebSocket:
        if self._index >= len(self._sockets):
            await asyncio.Event().wait()  # no further connections in this test
        gate = self.allow[self._index]
        socket = self._sockets[self._index]
        self._index += 1
        await gate.wait()
        socket.opened.set()
        return socket

    async def __aexit__(self, *_exc) -> bool:
        return False


def _client(config: OrchestratorClientConfig, held: HeldDeployment, **kwargs):
    return OrchestratorClient(
        config,
        haniel_version="0.1.0",
        deploy_approval_handler=held.handle,
        repo_snapshot_handler=_snapshot,
        **kwargs,
    )


def _pretend_connected(client: OrchestratorClient) -> SentLog:
    sent = SentLog()
    client._send_json = sent.send  # type: ignore[method-assign]
    client._connected = True
    client._ws = object()
    client._loop = asyncio.get_running_loop()
    return sent


class TestReceiveLoopIsNotBlockedByExecution:
    async def test_listen_reads_the_next_message_while_a_deploy_is_held(
        self, config
    ) -> None:
        held = HeldDeployment()
        commands: list[tuple[str, str]] = []

        def service_command(name: str, action: str, _payload) -> dict:
            commands.append((name, action))
            return {"ok": True}

        client = _client(config, held, service_command_handler=service_command)
        sent = _pretend_connected(client)
        client._sequencer.start()
        socket = FakeWebSocket()
        listener = asyncio.create_task(client._listen(socket))

        deploy_id = f"{config.node_id}:repo:main:{TARGET_A}"
        socket.deliver(_approval(config, TARGET_A, "attempt-1"))
        await held.wait_entered(deploy_id)
        socket.deliver(_service_command("cmd-1"))

        result = await sent.wait_for(
            lambda m: (
                m["type"] == "service_command_result" and m["command_id"] == "cmd-1"
            )
        )
        assert result["success"] is True
        assert commands == [("web", "start")]
        assert not held.release[deploy_id].is_set(), "deploy still held"

        held.let_go(deploy_id)
        await sent.wait_for(
            lambda m: m["type"] == "repo_reconciliation" and m["phase"] == "settled"
        )
        socket.drop()
        await asyncio.wait_for(listener, 2)
        await client._sequencer.close()

    async def test_plan_probe_and_service_command_are_answered_during_a_deploy(
        self, config
    ) -> None:
        held = HeldDeployment()
        planner_calls: list[str] = []

        def planner(probe: dict) -> dict:
            planner_calls.append(probe["probe_id"])
            return {
                "mode": "execute",
                "reason": "normal_pull",
                "error": None,
                "fingerprint": "fp-1",
                "current_head": TARGET_B,
                "journal_status": None,
                "journal_attempt_id": None,
                "linked_orchestrator_attempt_id": None,
                "journal_target_head": None,
                "original_previous_head": None,
                "manifest_identity": None,
                "manifest_digest": None,
                "journal_completed_at": None,
            }

        client = _client(
            config,
            held,
            service_command_handler=lambda *_: {"ok": True},
            deploy_plan_probe_handler=planner,
        )
        sent = _pretend_connected(client)
        client._sequencer.start()
        deploy_id = f"{config.node_id}:repo:main:{TARGET_A}"

        await client._handle_server_message(_approval(config, TARGET_A, "attempt-1"))
        await held.wait_entered(deploy_id)
        await client._handle_server_message(_plan_probe(config, "probe-1"))
        await client._handle_server_message(_service_command("cmd-1"))

        proposal = await sent.wait_for(
            lambda m: m["type"] == "deploy_plan_proposal" and m["probe_id"] == "probe-1"
        )
        assert proposal["mode"] == "execute"
        assert proposal["connection_generation"] == "generation-1"
        await sent.wait_for(
            lambda m: (
                m["type"] == "service_command_result" and m["command_id"] == "cmd-1"
            )
        )
        assert planner_calls == ["probe-1"]
        assert sent.of_type("deploy_result") == []

        held.let_go(deploy_id)
        await sent.wait_for(lambda m: m["type"] == "deploy_result")
        await client._sequencer.close()

    async def test_second_approval_waits_and_its_progress_starts_when_it_begins(
        self, config
    ) -> None:
        held = HeldDeployment()
        client = _client(config, held)
        sent = _pretend_connected(client)
        client._sequencer.start()
        deploy_a = f"{config.node_id}:repo:main:{TARGET_A}"
        deploy_b = f"{config.node_id}:repo:main:{TARGET_B}"

        await client._handle_server_message(_approval(config, TARGET_A, "attempt-1"))
        await held.wait_entered(deploy_a)
        await client._handle_server_message(_approval(config, TARGET_B, "attempt-2"))
        assert client._sequencer.queued == 1
        for _ in range(5):
            await asyncio.sleep(0)
        assert held.order == [deploy_a]
        assert [m["deploy_id"] for m in sent.of_type("deploy_progress")] == [deploy_a]

        held.let_go(deploy_a)
        await held.wait_entered(deploy_b)
        await sent.wait_for(
            lambda m: m["type"] == "deploy_progress" and m["deploy_id"] == deploy_b
        )
        results = await sent.wait_for(
            lambda m: m["type"] == "deploy_result" and m["deploy_id"] == deploy_a
        )
        assert results["orchestrator_attempt_id"] == "attempt-1"
        progress_b_index = next(
            i
            for i, m in enumerate(sent.messages)
            if m["type"] == "deploy_progress" and m["deploy_id"] == deploy_b
        )
        settled_a_index = next(
            i
            for i, m in enumerate(sent.messages)
            if m["type"] == "repo_reconciliation" and m["deploy_id"] == deploy_a
        )
        assert settled_a_index < progress_b_index
        assert held.order == [deploy_a, deploy_b]

        held.let_go(deploy_b)
        await sent.wait_for(
            lambda m: m["type"] == "deploy_result" and m["deploy_id"] == deploy_b
        )
        await client._sequencer.close()


class TestResultsSurviveTheSocket:
    def test_stop_closes_live_socket_and_finishes_the_sequencer(
        self, config, monkeypatch
    ) -> None:
        class ListeningSocket(FakeWebSocket):
            def __init__(self) -> None:
                super().__init__()
                self.listening = threading.Event()

            async def __anext__(self) -> str:
                self.listening.set()
                return await super().__anext__()

            async def close(self) -> None:
                self.drop()

        socket = ListeningSocket()
        monkeypatch.setattr("websockets.connect", FakeConnect([socket]))
        client = OrchestratorClient(config, haniel_version="0.1.0")
        sequencer_closed = threading.Event()
        close_sequencer = client._sequencer.close

        async def record_sequencer_close() -> None:
            await close_sequencer()
            sequencer_closed.set()

        monkeypatch.setattr(client._sequencer, "close", record_sequencer_close)
        stopper = threading.Thread(target=client.stop)
        client.start()
        try:
            assert socket.listening.wait(timeout=2)
            stopper.start()
            stopper.join(timeout=2)
            assert not stopper.is_alive(), "stop waited for the thread timeout"
            assert sequencer_closed.is_set()
            assert client._thread is not None and not client._thread.is_alive()
        finally:
            if client._loop is not None:
                client._loop.call_soon_threadsafe(socket.drop)
            if stopper.ident is not None:
                stopper.join(timeout=2)
            if client._thread is not None:
                client._thread.join(timeout=2)

    async def test_execution_survives_disconnect_and_results_flush_on_reconnect(
        self, config, monkeypatch
    ) -> None:
        held = HeldDeployment()
        client = _client(config, held)
        client._loop = asyncio.get_running_loop()
        first, second = FakeWebSocket(), FakeWebSocket()
        connect = FakeConnect([first, second])
        connect.allow[1].clear()  # hold the reconnect until the deploy finished
        monkeypatch.setattr("websockets.connect", connect)
        run = asyncio.create_task(client._run())
        deploy_id = f"{config.node_id}:repo:main:{TARGET_A}"
        try:
            await self._survives_disconnect(
                client, connect, first, second, held, deploy_id
            )
        finally:
            held.let_go(deploy_id)
            client._stop_event.set()
            connect.allow[1].set()
            first.drop()
            second.drop()
            await _finish(run)

    @staticmethod
    async def _survives_disconnect(
        client, connect, first, second, held, deploy_id
    ) -> None:
        await asyncio.wait_for(first.opened.wait(), 2)
        await first.sent.wait_for(lambda m: m["type"] == "node_hello")
        first.deliver(_approval(client._config, TARGET_A, "attempt-1"))
        await held.wait_entered(deploy_id)
        await first.sent.wait_for(lambda m: m["type"] == "deploy_progress")
        assert [m["type"] for m in first.sent.messages] == [
            "node_hello",
            "deploy_progress",
        ]

        first.drop()
        await asyncio.wait_for(_until(lambda: client._ws is None), 2)
        assert client._sequencer.executing is not None, "execution was cancelled"

        held.let_go(deploy_id)
        await asyncio.wait_for(
            _until(lambda: len(client._pending_deploy_results) == 2), 5
        )
        assert [m["type"] for m in client._pending_deploy_results] == [
            "deploy_result",
            "repo_reconciliation",
        ]
        assert not first.closed or len(first.sent.messages) == 2

        connect.allow[1].set()
        await second.sent.wait_for(
            lambda m: m["type"] == "repo_reconciliation" and m["phase"] == "settled"
        )
        assert [m["type"] for m in second.sent.messages] == [
            "node_hello",
            "deploy_result",
            "repo_reconciliation",
        ]
        result, settled = second.sent.messages[1], second.sent.messages[2]
        assert result["status"] == "success"
        assert result["orchestrator_attempt_id"] == "attempt-1"
        assert result["connection_generation"] == "generation-1"
        assert settled["orchestrator_attempt_id"] == "attempt-1"
        assert settled["connection_generation"] == "generation-1"
        assert client._pending_deploy_results == []

    async def test_stop_drops_queued_approvals_with_a_log(
        self, config, monkeypatch, caplog
    ) -> None:
        held = HeldDeployment()
        client = _client(config, held)
        client._loop = asyncio.get_running_loop()
        socket = FakeWebSocket()
        monkeypatch.setattr("websockets.connect", FakeConnect([socket]))
        run = asyncio.create_task(client._run())
        deploy_a = f"{config.node_id}:repo:main:{TARGET_A}"
        try:
            await asyncio.wait_for(socket.opened.wait(), 2)
            socket.deliver(_approval(config, TARGET_A, "attempt-1"))
            await held.wait_entered(deploy_a)
            socket.deliver(_approval(config, TARGET_B, "attempt-2"))
            await asyncio.wait_for(_until(lambda: client._sequencer.queued == 1), 2)

            with caplog.at_level(logging.WARNING):
                client._stop_event.set()
                socket.drop()
                await asyncio.wait_for(run, 5)

            assert "attempt-2" in caplog.text
            assert held.order == [deploy_a]
        finally:
            held.let_go(deploy_a)
            client._stop_event.set()
            socket.drop()
            await _finish(run)

    async def test_auto_deploy_report_is_buffered_when_disconnected(
        self, config
    ) -> None:
        client = OrchestratorClient(config, haniel_version="0.1.0")
        deploy_id = f"{config.node_id}:repo:main:{TARGET_A}"
        permit = {
            "deploy_id": deploy_id,
            "begun_orchestrator_attempt_id": "auto-1",
            "connection_generation": "generation-7",
        }

        client.report_deploy_attempt(
            _snapshot("repo", "main", deploy_id), None, 12, permit
        )

        assert [m["type"] for m in client._pending_deploy_results] == [
            "deploy_result",
            "repo_reconciliation",
        ]
        result, settled = client._pending_deploy_results
        assert result["status"] == "success"
        assert result["duration_ms"] == 12
        assert result["orchestrator_attempt_id"] == "auto-1"
        assert result["connection_generation"] == "generation-7"
        assert settled["phase"] == "settled"
        assert settled["orchestrator_attempt_id"] == "auto-1"
        assert settled["connection_generation"] == "generation-7"

    async def test_revalidation_terminal_is_buffered_when_disconnected(
        self, config
    ) -> None:
        def handler(_approval: dict, _progress) -> None:
            raise ApprovalRevalidationError("already consumed")

        client = OrchestratorClient(
            config,
            haniel_version="0.1.0",
            deploy_approval_handler=handler,
            repo_snapshot_handler=_snapshot,
        )
        await client._handle_deploy_approval(_approval(config, TARGET_A, "attempt-1"))

        assert [m["type"] for m in client._pending_deploy_results] == [
            "deploy_attempt_terminal"
        ]
        terminal = client._pending_deploy_results[0]
        assert terminal["kind"] == "approval_revalidation_failed"
        assert terminal["orchestrator_attempt_id"] == "attempt-1"
        assert terminal["connection_generation"] == "generation-1"


class TestConnectionBoundAnswers:
    async def test_plan_proposal_is_dropped_when_the_connection_changed(
        self, config, caplog
    ) -> None:
        entered = threading.Event()
        release = threading.Event()

        def planner(_probe: dict) -> dict:
            entered.set()
            release.wait(timeout=5)
            return {"mode": "execute", "reason": "normal_pull"}

        client = OrchestratorClient(
            config, haniel_version="0.1.0", deploy_plan_probe_handler=planner
        )
        sent = _pretend_connected(client)
        await client._handle_server_message(_plan_probe(config, "probe-1"))
        assert await asyncio.to_thread(entered.wait, 2)
        client._connection_serial += 1  # the socket that asked is gone
        with caplog.at_level(logging.INFO):
            release.set()
            await asyncio.gather(*client._background_tasks)

        assert sent.of_type("deploy_plan_proposal") == []
        assert "probe-1" in caplog.text

    async def test_service_command_result_is_dropped_with_a_log_without_a_socket(
        self, config, caplog
    ) -> None:
        entered = threading.Event()
        release = threading.Event()

        def service_command(_name: str, _action: str, _payload) -> dict:
            entered.set()
            release.wait(timeout=5)
            return {"ok": True}

        client = OrchestratorClient(
            config, haniel_version="0.1.0", service_command_handler=service_command
        )
        client._connected = True
        client._loop = asyncio.get_running_loop()
        await client._handle_server_message(_service_command("cmd-9"))
        assert await asyncio.to_thread(entered.wait, 2)
        client._connected = False
        client._ws = None
        with caplog.at_level(logging.WARNING):
            release.set()
            await asyncio.gather(*client._background_tasks)

        assert "cmd-9" in caplog.text


async def _until(predicate: Callable[[], bool]) -> None:
    while not predicate():
        await asyncio.sleep(0.005)


async def _finish(run: asyncio.Task) -> None:
    """Let the client's main task end; cancel it if stopping did not suffice."""
    try:
        await asyncio.wait_for(run, 5)
    except asyncio.CancelledError:
        pass
