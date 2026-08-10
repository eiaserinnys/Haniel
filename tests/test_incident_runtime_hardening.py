"""End-to-end regressions for the 260810 release and config-lock incident."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from haniel.config import HanielConfig, RepoConfig, ServiceConfig, load_config
from haniel.config.model import OrchestratorClientConfig
from haniel.core.deployment_state import DeploymentStateStore
from haniel.core.deployment_errors import StableDeploymentError
from haniel.core.git import fetch_repo, get_head, get_remote_head
from haniel.core.runner import ServiceRunner
from haniel.core.runner_config_snapshot import RepoObservation
from haniel.integrations.deploy_attempt_gate import DeployPermissionError
from haniel.integrations.orchestrator_client import OrchestratorClient


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _python_command(script: Path) -> str:
    argv = [sys.executable, str(script)]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _process_is_running(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_timeout = 0x00000102
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def _release_manifest(failing_command: str, *, timeout_seconds: int = 30) -> dict:
    def command(name: str, value: str, timeout: int = 30) -> dict:
        return {"name": name, "command": value, "timeout_seconds": timeout}

    return {
        "schema_version": "haniel.release.v1",
        "release_id": "incident-release",
        "migration": {
            "preflight": command("preflight", _python_command(Path(sys.executable))),
            "apply": command("apply", _python_command(Path(sys.executable))),
            "provenance_probe": {
                "prepare": command("prepare", failing_command, timeout_seconds),
                "probe": command("probe", failing_command, timeout_seconds),
            },
        },
        "post_start_verify": [command("health", _python_command(Path(sys.executable)))],
        "recovery": {
            "strategy": "rollback",
            "command": command("restore", _python_command(Path(sys.executable))),
        },
    }


def _remote_with_update(
    tmp_path: Path,
    name: str,
    *,
    manifest: dict | None = None,
) -> tuple[Path, Path, str]:
    source = tmp_path / f"{name}-source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test User")
    (source / "value.txt").write_text("old", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "old")
    remote = tmp_path / f"{name}.git"
    subprocess.run(["git", "clone", "--bare", str(source), str(remote)], check=True)
    live = tmp_path / f"{name}-live"
    subprocess.run(["git", "clone", str(remote), str(live)], check=True)
    previous = get_head(live)
    if manifest is not None:
        deploy = source / "deploy"
        deploy.mkdir()
        (deploy / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source / "value.txt").write_text("new", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "new")
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "origin", "main")
    return live, remote, previous


def _runner_with_failed_and_healthy_repo(
    tmp_path: Path,
    failing_command: str,
    *,
    timeout_seconds: int = 30,
    with_service: bool = False,
) -> tuple[ServiceRunner, Path, Path, Path]:
    failed_live, failed_remote, failed_head = _remote_with_update(
        tmp_path,
        "failed",
        manifest=_release_manifest(
            failing_command,
            timeout_seconds=timeout_seconds,
        ),
    )
    healthy_live, healthy_remote, _healthy_head = _remote_with_update(
        tmp_path, "healthy"
    )
    service_script = tmp_path / "service.py"
    service_script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    services = (
        {
            "healthy-service": ServiceConfig(
                run=_python_command(service_script),
                repo="healthy",
            )
        }
        if with_service
        else {}
    )
    runner = ServiceRunner(
        HanielConfig(
            auto_apply=False,
            repos={
                "failed": RepoConfig(
                    url=str(failed_remote),
                    path=str(failed_live),
                    release_manifest="deploy/release.json",
                ),
                "healthy": RepoConfig(url=str(healthy_remote), path=str(healthy_live)),
            },
            services=services,
        ),
        config_dir=tmp_path,
    )
    return runner, failed_live, healthy_live, Path(failed_head)


def test_startup_nonzero_release_is_isolated_and_next_repo_and_service_continue(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    failure = tmp_path / "fail.py"
    failure.write_text("raise SystemExit(9)\n", encoding="utf-8")
    runner, failed_live, healthy_live, _ = _runner_with_failed_and_healthy_repo(
        tmp_path, _python_command(failure), with_service=True
    )
    failed_head = get_head(failed_live)

    try:
        runner._apply_startup_updates()
        runner.start_services()
        journal = DeploymentStateStore(tmp_path / ".haniel" / "deployments").read(
            "failed"
        )
        assert journal is not None
        assert journal["state"] == "failed"
        assert journal["error_code"] == "COMMAND_EXIT_NONZERO"
        assert "COMMAND_EXIT_NONZERO" in caplog.text
        assert get_head(failed_live) == failed_head
        assert get_head(healthy_live) == get_remote_head(healthy_live, "main")
        assert runner.process_manager.is_running("healthy-service")
    finally:
        runner.process_manager.stop_all(timeout=2)


def test_startup_timeout_is_isolated_reaps_child_and_next_repo_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pid_file = tmp_path / "timeout.pid"
    failure = tmp_path / "timeout.py"
    failure.write_text(
        "import os, pathlib, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    runner, _failed_live, healthy_live, _ = _runner_with_failed_and_healthy_repo(
        tmp_path,
        _python_command(failure),
        timeout_seconds=1,
    )

    runner._apply_startup_updates()

    journal = DeploymentStateStore(tmp_path / ".haniel" / "deployments").read("failed")
    assert journal is not None
    assert journal["state"] == "failed"
    assert journal["error_code"] == "COMMAND_TIMEOUT"
    assert "COMMAND_TIMEOUT" in caplog.text
    assert get_head(healthy_live) == get_remote_head(healthy_live, "main")
    pid = int(pid_file.read_text(encoding="utf-8"))
    assert not _process_is_running(pid)


def test_poll_git_barrier_does_not_block_reload_or_commit_stale_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_live, old_remote, _ = _remote_with_update(tmp_path, "old")
    new_live, new_remote, _ = _remote_with_update(tmp_path, "new")
    config_path = tmp_path / "haniel.yaml"

    def write_config(path: Path, remote: Path) -> None:
        config_path.write_text(
            f"auto_apply: false\nrepos:\n  app:\n    url: {remote}\n    path: {path}\n",
            encoding="utf-8",
        )

    write_config(old_live, old_remote)
    runner = ServiceRunner(
        load_config(config_path), config_dir=tmp_path, config_path=config_path
    )
    fetch_entered = threading.Event()
    release_fetch = threading.Event()

    def blocking_fetch(*, path: Path, branch: str) -> bool:
        fetch_entered.set()
        assert release_fetch.wait(timeout=5)
        return fetch_repo(path=path, branch=branch)

    monkeypatch.setattr("haniel.core.runner.fetch_repo", blocking_fetch)
    poll = threading.Thread(target=runner._poll_cycle)
    poll.start()
    assert fetch_entered.wait(timeout=5)
    write_config(new_live, new_remote)
    reload_done = threading.Event()

    def reload_config() -> None:
        runner.reload_config()
        reload_done.set()

    reload_thread = threading.Thread(target=reload_config)
    reload_thread.start()
    try:
        assert reload_done.wait(timeout=1), "reload blocked behind poll git I/O"
    finally:
        release_fetch.set()
        poll.join(timeout=5)
        reload_thread.join(timeout=5)

    assert not poll.is_alive()
    assert not reload_thread.is_alive()
    assert runner.config.repos["app"].path == str(new_live)
    assert runner._repo_states["app"].config.path == str(new_live)
    assert runner._repo_states["app"].pending_changes is None


def test_config_replace_preserves_observation_committed_during_new_repo_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "existing"
    added = tmp_path / "added"
    existing.mkdir()
    added.mkdir()
    runner = ServiceRunner(
        HanielConfig(repos={"existing": RepoConfig(url="unused", path=str(existing))}),
        config_dir=tmp_path,
    )
    original = runner._snapshot_repo_runtime("existing")
    candidate = HanielConfig(
        repos={
            "existing": original.config,
            "added": RepoConfig(url="unused", path=str(added)),
        }
    )
    probe_entered = threading.Event()
    release_probe = threading.Event()

    def blocking_head(path: Path) -> str:
        assert path == added
        probe_entered.set()
        assert release_probe.wait(timeout=5)
        return "added-head"

    monkeypatch.setattr("haniel.core.runner.get_head", blocking_head)
    errors: list[BaseException] = []

    def replace() -> None:
        try:
            runner._replace_config_snapshot(candidate, original.generation)
        except BaseException as error:
            errors.append(error)

    replacement = threading.Thread(target=replace)
    replacement.start()
    assert probe_entered.wait(timeout=2)
    observed_at = datetime.now()
    assert runner._commit_repo_observation(
        RepoObservation(
            generation=original.generation,
            repo_name="existing",
            repo_config=original.config,
            last_fetch=observed_at,
            fetch_error=None,
            last_head="fresh-observation",
            pending_changes={"commits": ["fresh"]},
            changed=True,
        )
    )
    release_probe.set()
    replacement.join(timeout=5)

    assert not replacement.is_alive()
    assert errors == []
    current = runner._snapshot_repo_runtime("existing")
    assert current.last_head == "fresh-observation"
    assert current.last_fetch == observed_at
    assert current.pending_changes == {"commits": ["fresh"]}


def test_config_replace_reobserves_changed_repo_identity(tmp_path: Path) -> None:
    old = tmp_path / "old-identity"
    new = tmp_path / "new-identity"
    for path, value in ((old, "old"), (new, "new")):
        path.mkdir()
        _git(path, "init", "-b", "main")
        _git(path, "config", "user.email", "test@example.com")
        _git(path, "config", "user.name", "Test User")
        (path / "identity.txt").write_text(value, encoding="utf-8")
        _git(path, "add", ".")
        _git(path, "commit", "-m", value)

    runner = ServiceRunner(
        HanielConfig(repos={"app": RepoConfig(url="old", path=str(old))}),
        config_dir=tmp_path,
    )
    runner._repo_states["app"].last_head = "stale-old-observation"
    runner._repo_states["app"].pending_changes = {"commits": ["stale"]}
    snapshot = runner._snapshot_config_state()

    runner._replace_config_snapshot(
        HanielConfig(repos={"app": RepoConfig(url="new", path=str(new))}),
        snapshot.generation,
    )

    current = runner._snapshot_repo_runtime("app")
    assert current.last_head == get_head(new)
    assert current.pending_changes is None
    assert current.last_fetch is None


def test_repo_and_config_snapshot_are_one_generation_under_reload(
    tmp_path: Path,
) -> None:
    runner = ServiceRunner(
        HanielConfig(repos={"app": RepoConfig(url="old", path="old")}),
        config_dir=tmp_path,
    )
    failures: list[tuple[int, int]] = []
    stop = threading.Event()

    def replace_repeatedly() -> None:
        for index in range(100):
            snapshot = runner._snapshot_config_state()
            runner._replace_config_snapshot(
                HanielConfig(
                    repos={
                        "app": RepoConfig(
                            url=f"generation-{index}",
                            path=f"repo-{index}",
                        )
                    }
                ),
                snapshot.generation,
            )
        stop.set()

    writer = threading.Thread(target=replace_repeatedly)
    writer.start()
    while not stop.is_set():
        config_snapshot, repo_snapshot = runner._snapshot_repo_and_config("app")
        if config_snapshot.generation != repo_snapshot.generation:
            failures.append((config_snapshot.generation, repo_snapshot.generation))
    writer.join(timeout=5)

    assert not writer.is_alive()
    assert failures == []


def test_manual_manifest_activation_rolls_back_when_generation_commit_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, remote, previous = _remote_with_update(
        tmp_path,
        "manual-stale",
        manifest=_release_manifest(_python_command(Path(sys.executable))),
    )
    fetch_repo(path=live, branch="main")
    target = get_remote_head(live, "main")
    runner = ServiceRunner(
        HanielConfig(
            repos={
                "app": RepoConfig(
                    url=str(remote),
                    path=str(live),
                    release_manifest="deploy/release.json",
                )
            }
        ),
        config_dir=tmp_path,
    )
    runner._repo_states["app"].pending_changes = {"commits": [target]}
    staged = SimpleNamespace(
        target_head=target,
        manifest_digest="d" * 64,
        manifest=SimpleNamespace(release_id="incident-release"),
    )
    monkeypatch.setattr(
        "haniel.core.runner.probe_manifest_target", lambda *_a, **_k: staged
    )
    monkeypatch.setattr(runner, "_commit_repo_observation", lambda _observation: False)
    deploy = MagicMock()
    monkeypatch.setattr("haniel.core.runner.run_manifest_deployment", deploy)

    with pytest.raises(StableDeploymentError) as exc_info:
        runner.trigger_pull("app")

    assert exc_info.value.code == "CONFIG_GENERATION_CHANGED"
    assert get_head(live) == previous
    deploy.assert_not_called()


def test_startup_manifest_activation_rolls_back_and_service_continues_on_stale_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, remote, previous = _remote_with_update(
        tmp_path,
        "startup-stale",
        manifest=_release_manifest(_python_command(Path(sys.executable))),
    )
    runner = ServiceRunner(
        HanielConfig(
            repos={
                "app": RepoConfig(
                    url=str(remote),
                    path=str(live),
                    release_manifest="deploy/release.json",
                )
            },
            services={"app-service": ServiceConfig(run="unused", repo="app")},
        ),
        config_dir=tmp_path,
    )
    target = _git(remote, "rev-parse", "main")
    staged = SimpleNamespace(
        target_head=target,
        manifest_digest="d" * 64,
        manifest=SimpleNamespace(release_id="incident-release"),
    )
    monkeypatch.setattr(
        "haniel.core.runner.probe_manifest_target", lambda *_a, **_k: staged
    )
    monkeypatch.setattr(runner, "_commit_repo_observation", lambda _observation: False)
    start_service = MagicMock(return_value=True)
    monkeypatch.setattr(runner, "_start_service", start_service)
    deploy = MagicMock()
    monkeypatch.setattr("haniel.core.runner.run_manifest_deployment", deploy)

    runner._apply_startup_updates()
    runner.start_services()

    assert get_head(live) == previous
    deploy.assert_not_called()
    start_service.assert_called_once_with("app-service")


def test_auto_poll_isolates_only_typed_operational_repo_errors(tmp_path: Path) -> None:
    runner = ServiceRunner(
        HanielConfig(
            repos={
                "failed": RepoConfig(url="unused", path="failed"),
                "healthy": RepoConfig(url="unused", path="healthy"),
            }
        ),
        config_dir=tmp_path,
    )
    runner._run_auto_deploy = MagicMock(
        side_effect=[
            StableDeploymentError("COMMAND_EXIT_NONZERO", "release child failed"),
            None,
        ]
    )

    runner._apply_changes(["failed", "healthy"])

    assert [entry.args[0] for entry in runner._run_auto_deploy.call_args_list] == [
        "failed",
        "healthy",
    ]

    runner._run_auto_deploy.reset_mock(side_effect=True)
    runner._run_auto_deploy.side_effect = RuntimeError("programming defect")
    with pytest.raises(RuntimeError, match="programming defect"):
        runner._apply_changes(["failed", "healthy"])
    runner._run_auto_deploy.assert_called_once_with("failed")


def test_poll_loop_does_not_hide_programming_runtime_error(tmp_path: Path) -> None:
    runner = ServiceRunner(HanielConfig(), config_dir=tmp_path)

    def programming_failure() -> None:
        runner._stop_event.set()
        raise RuntimeError("programming defect")

    runner._poll_cycle = programming_failure  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="programming defect"):
        runner._poll_loop()


def test_poll_report_timeout_keeps_heartbeat_node_hello_reader_and_pong_alive(
    tmp_path: Path,
) -> None:
    live, remote, previous = _remote_with_update(tmp_path, "reported")
    orch_config = OrchestratorClientConfig(
        url="ws://localhost:9300/ws/node",
        token="test-token",
        node_id="test-node",
        ping_timeout=0.2,
    )
    runner = ServiceRunner(
        HanielConfig(
            auto_apply=False,
            repos={"app": RepoConfig(url=str(remote), path=str(live))},
            orchestrator_client=orch_config,
        ),
        config_dir=tmp_path,
    )
    runner._repo_states["app"].last_head = previous
    client = OrchestratorClient(
        orch_config,
        haniel_version="test",
        get_services_info=runner._collect_services_info,
    )
    runner._orch_client = client

    class BarrierWebSocket:
        def __init__(self) -> None:
            self.messages: asyncio.Queue[str | None] = asyncio.Queue()
            self.report_entered = asyncio.Event()
            self.release_report = asyncio.Event()
            self.sent: list[str] = []
            self.ponged = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self) -> str:
            message = await self.messages.get()
            if message is None:
                raise StopAsyncIteration
            return message

        async def send(self, payload: str) -> None:
            message_type = json.loads(payload)["type"]
            self.sent.append(message_type)
            if message_type not in {"node_status", "node_hello"}:
                self.report_entered.set()
                await self.release_report.wait()

        async def ping(self) -> None:
            self.ponged.set()

    async def scenario() -> None:
        ws = BarrierWebSocket()
        client._ws = ws
        client._connected = True
        client._loop = asyncio.get_running_loop()
        reader_progressed = asyncio.Event()
        original_handler = client._handle_server_message

        async def tracked_handler(message: dict) -> None:
            await original_handler(message)
            reader_progressed.set()

        client._handle_server_message = tracked_handler  # type: ignore[method-assign]
        listener = asyncio.create_task(client._listen(ws))
        poll = threading.Thread(target=runner._poll_cycle)
        poll.start()
        await asyncio.wait_for(ws.report_entered.wait(), timeout=5)
        await asyncio.to_thread(poll.join, 2)
        assert not poll.is_alive(), "poll did not escape the timed-out report send"

        heartbeat = asyncio.create_task(client._send_heartbeat())
        services = await asyncio.wait_for(client._services_snapshot(), timeout=1)
        await asyncio.wait_for(
            client._send_json(client._build_node_hello(services)), timeout=1
        )
        await ws.messages.put(
            json.dumps({"type": "deploy_reject", "deploy_id": "d", "reason": "test"})
        )
        await asyncio.wait_for(reader_progressed.wait(), timeout=1)
        await asyncio.wait_for(ws.ping(), timeout=1)
        await asyncio.wait_for(heartbeat, timeout=1)

        assert {"node_status", "node_hello"} <= set(ws.sent)
        assert ws.ponged.is_set()
        await ws.messages.put(None)
        await asyncio.wait_for(listener, timeout=1)

    asyncio.run(scenario())


def test_reconnect_generation_rejects_stale_ack_and_accepts_new_generation() -> None:
    config = OrchestratorClientConfig(
        url="ws://localhost:9300/ws/node",
        token="test-token",
        node_id="test-node",
    )
    client = OrchestratorClient(config, haniel_version="test")
    gate = client._deploy_attempt_gate
    gate.observe_generation("old")
    gate.register("old-attempt", "deploy")
    gate.reset_connection()
    gate.observe_generation("new")

    assert not gate.accept_ack(
        {
            "type": "deploy_attempt_ack",
            "accepted": True,
            "requested_orchestrator_attempt_id": "old-attempt",
            "begun_orchestrator_attempt_id": "old-attempt",
            "deploy_id": "deploy",
            "connection_generation": "old",
            "probe_id": "old-probe",
            "execution_mode": "execute",
            "preflight_fingerprint": "old-fingerprint",
        }
    )
    with pytest.raises(DeployPermissionError, match="connection_generation_changed"):
        gate.wait("old-attempt", 0.1)

    gate.register("new-attempt", "deploy")
    assert gate.accept_ack(
        {
            "type": "deploy_attempt_ack",
            "accepted": True,
            "requested_orchestrator_attempt_id": "new-attempt",
            "begun_orchestrator_attempt_id": "new-attempt",
            "deploy_id": "deploy",
            "connection_generation": "new",
            "probe_id": "new-probe",
            "execution_mode": "execute",
            "preflight_fingerprint": "new-fingerprint",
        }
    )
    assert gate.wait("new-attempt", 0.1)["connection_generation"] == "new"
