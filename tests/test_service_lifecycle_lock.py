"""Service lifecycle commands and deployments serialize per service (#1399)."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from haniel.config.model import HanielConfig
from haniel.core.process import ProcessManager
from haniel.core.runner import ServiceRunner
from haniel.core.service_locks import ServiceLockRegistry

RECORD_SCRIPT = Path(__file__).parent / "fixtures" / "record_pid_and_sleep.py"


def _runner(tmp_path: Path, pid_file: Path) -> ServiceRunner:
    payload = {
        "poll_interval": 10,
        "repos": {
            "app": {
                "url": "git@github.com:test/app.git",
                "path": "app",
                "branch": "main",
            },
        },
        "services": {
            "web": {
                "run": f"{sys.executable} {RECORD_SCRIPT} {pid_file}",
                "repo": "app",
            },
        },
    }
    config = HanielConfig.model_validate(payload)
    return ServiceRunner(config=config, config_dir=tmp_path)


def _pid_lines(pid_file: Path) -> list[str]:
    if not pid_file.exists():
        return []
    return pid_file.read_text(encoding="utf-8").split()


def _wait_for_recorded_pids(pid_file: Path, minimum: int = 1) -> None:
    deadline = time.time() + 10
    while time.time() < deadline and len(_pid_lines(pid_file)) < minimum:
        time.sleep(0.05)


def _cleanup(runner: ServiceRunner, pid_file: Path) -> None:
    runner.process_manager.stop_service("web", force=True)
    for line in _pid_lines(pid_file):
        try:
            os.kill(int(line), signal.SIGTERM)
        except (OSError, ValueError):
            pass


def test_registry_holds_are_exclusive_and_reentrant() -> None:
    registry = ServiceLockRegistry()
    attempts: list[bool] = []

    def attempt() -> None:
        with registry.try_hold("a") as acquired:
            attempts.append(acquired)

    with registry.hold(["b", "a"]):
        with registry.hold("a"):
            blocked = threading.Thread(target=attempt, daemon=True)
            blocked.start()
            blocked.join(timeout=1)
            assert attempts == [False]

    open_attempt = threading.Thread(target=attempt, daemon=True)
    open_attempt.start()
    open_attempt.join(timeout=5)
    assert attempts == [False, True]


def test_concurrent_starts_spawn_one_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "pids.txt"
    runner = _runner(tmp_path, pid_file)
    original_spawn = ProcessManager._spawn_process

    def delayed_spawn(self: ProcessManager, *args, **kwargs):
        # Widen the check-to-Popen window so overlapping starts are certain.
        time.sleep(0.3)
        return original_spawn(self, *args, **kwargs)

    monkeypatch.setattr(ProcessManager, "_spawn_process", delayed_spawn)

    barrier = threading.Barrier(2)
    results: list[bool] = []

    def start() -> None:
        barrier.wait(timeout=10)
        results.append(runner._start_service("web"))

    threads = [threading.Thread(target=start, daemon=True) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert sorted(results) == [False, True]
        _wait_for_recorded_pids(pid_file)
        time.sleep(0.5)  # A second, overlapping child would have recorded by now.
        recorded = _pid_lines(pid_file)
        assert len(recorded) == 1
        assert runner.process_manager.get_pid("web") == int(recorded[0])
        with runner._restart_lock:
            assert "web" not in runner._pending_restarts

        config = runner._snapshot_config_state().enabled_services["web"]
        with pytest.raises(RuntimeError, match="already running"):
            runner.process_manager.start_service("web", config)
    finally:
        _cleanup(runner, pid_file)


def test_restart_blocks_deploy_services_until_it_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "pids.txt"
    runner = _runner(tmp_path, pid_file)
    runner._repo_states["app"].pending_changes = {"commits": ["a"]}
    monkeypatch.setattr(runner, "_pull_repo", lambda repo_name: (True, []))

    events: list[str] = []
    events_lock = threading.Lock()
    stop_gate_entered = threading.Event()
    stop_gate_release = threading.Event()
    stop_calls = 0

    def record(event: str) -> None:
        with events_lock:
            events.append(event)

    original_stop = ProcessManager.stop_service
    original_start = ProcessManager.start_service

    def gated_stop(self: ProcessManager, name: str, *args, **kwargs):
        nonlocal stop_calls
        with events_lock:
            index = stop_calls
            stop_calls += 1
        if index == 0:
            record(f"stop:{threading.current_thread().name}")
            stop_gate_entered.set()
            assert stop_gate_release.wait(timeout=10)
        else:
            record(f"stop:{threading.current_thread().name}")
        return original_stop(self, name, *args, **kwargs)

    def recording_start(self: ProcessManager, name: str, *args, **kwargs):
        record(f"start:{threading.current_thread().name}")
        return original_start(self, name, *args, **kwargs)

    monkeypatch.setattr(ProcessManager, "stop_service", gated_stop)
    monkeypatch.setattr(ProcessManager, "start_service", recording_start)

    try:
        assert runner._start_service("web") is True
        _wait_for_recorded_pids(pid_file)
        with events_lock:
            events.clear()
        time.sleep(0.2)

        restart_errors: list[BaseException] = []
        deploy_errors: list[BaseException] = []

        def restart() -> None:
            try:
                runner.restart_service("web")
                record("restart_done")
            except BaseException as error:  # pragma: no cover - failure evidence
                restart_errors.append(error)

        def deploy() -> None:
            try:
                runner.trigger_pull("app")
            except BaseException as error:  # pragma: no cover - failure evidence
                deploy_errors.append(error)

        restart_thread = threading.Thread(target=restart, name="restart", daemon=True)
        restart_thread.start()
        assert stop_gate_entered.wait(timeout=10)

        deploy_thread = threading.Thread(target=deploy, name="deploy", daemon=True)
        deploy_thread.start()
        time.sleep(0.4)
        assert events == ["stop:restart"], (
            "deployment touched the service while restart was still running"
        )

        stop_gate_release.set()
        restart_thread.join(timeout=30)
        deploy_thread.join(timeout=30)

        assert restart_thread.is_alive() is False
        assert deploy_thread.is_alive() is False
        assert restart_errors == []
        assert deploy_errors == []
        assert events == [
            "stop:restart",
            "start:restart",
            "restart_done",
            "stop:deploy",
            "start:deploy",
        ]
    finally:
        stop_gate_release.set()
        _cleanup(runner, pid_file)


def test_pending_restart_defers_while_service_lock_is_busy(tmp_path: Path) -> None:
    pid_file = tmp_path / "pids.txt"
    runner = _runner(tmp_path, pid_file)
    with runner._restart_lock:
        runner._pending_restarts["web"] = time.time() - 1

    holder_inside = threading.Event()
    holder_release = threading.Event()

    def hold_service() -> None:
        with runner.service_locks.hold("web"):
            holder_inside.set()
            holder_release.wait(timeout=10)

    holder = threading.Thread(target=hold_service, name="holder", daemon=True)
    holder.start()
    try:
        assert holder_inside.wait(timeout=5)
        runner._process_pending_restarts()
        assert not runner.process_manager.is_running("web")
        with runner._restart_lock:
            assert "web" in runner._pending_restarts
    finally:
        holder_release.set()
        holder.join(timeout=10)

    try:
        runner._process_pending_restarts()
        _wait_for_recorded_pids(pid_file)
        assert runner.process_manager.is_running("web")
    finally:
        _cleanup(runner, pid_file)


def test_dashboard_restart_uses_the_runner_restart() -> None:
    from haniel.dashboard import setup_dashboard

    runner = MagicMock()
    runner.get_status.return_value = {
        "services": {"web": {"state": "running"}},
        "repos": {},
    }
    routes, middleware, _ws_handler = setup_dashboard(runner)
    client = TestClient(Starlette(routes=routes, middleware=middleware))

    response = client.post("/api/services/web/restart")

    assert response.status_code == 200
    runner.restart_service.assert_called_once_with("web")
    runner.process_manager.stop_service.assert_not_called()
    runner._start_service.assert_not_called()


async def test_mcp_restart_uses_the_runner_restart() -> None:
    from haniel.integrations.mcp_server import HanielMcpServer

    runner = MagicMock()
    runner.get_status.return_value = {
        "services": {"web": {"state": "running"}},
        "repos": {},
    }
    server = HanielMcpServer(runner)

    result = await server._restart_service("web")

    assert "restarted" in result.lower()
    runner.restart_service.assert_called_once_with("web")
    runner.process_manager.stop_service.assert_not_called()
    runner._start_service.assert_not_called()
