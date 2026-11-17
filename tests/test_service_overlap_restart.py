"""Overlap declarations, planning and real process handover contracts."""

import json
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from haniel.config import ServiceConfig
from haniel.core.process import ProcessManager
from haniel.core.release_manifest import ReleaseManifest
from haniel.core.restart_filter import plan_restart_skips
from haniel.core.release_manifest import ServiceRestartFilter


def manifest(strategy=None):
    value = {
        "schema_version": "haniel.release.v1",
        "release_id": "one",
        "post_start_verify": [{"name": "verify", "command": "true"}],
        "recovery": {
            "strategy": "rollback",
            "command": {"name": "restore", "command": "true"},
        },
    }
    if strategy is not None:
        value["service_restart_strategies"] = {"app": strategy}
    return ReleaseManifest.model_validate(value)


def test_manifest_strategy_validation():
    assert manifest().service_restart_strategies == {}
    assert (
        manifest({"mode": "overlap"}).service_restart_strategies["app"].exit_timeout_sec
        == 60
    )
    for strategy in (
        {"mode": "other"},
        {"mode": "overlap", "exit_timeout_sec": 0},
        {"mode": "overlap", "other": True},
    ):
        with pytest.raises(ValidationError):
            manifest(strategy)


def test_overlap_dependency_does_not_force_filtered_service_restart():
    assert plan_restart_skips(
        affected=["app", "worker"],
        filters={"worker": ServiceRestartFilter(ignore_paths=["src/*"])},
        changed_paths=["src/app.py"],
        dependencies={"worker": ["app"]},
        overlapping=frozenset({"app"}),
    ) == frozenset({"worker"})


@pytest.mark.skipif(
    sys.platform != "linux", reason="Linux SO_REUSEPORT and /proc ownership"
)
def test_real_overlap_uses_new_port_owner_and_keeps_new_handle(tmp_path: Path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    script = tmp_path / "server.py"
    script.write_text(
        """import socket, sys, time\nfrom pathlib import Path\ns = socket.socket()\ns.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)\nif Path(sys.argv[2]).exists(): time.sleep(0.5)\ns.bind(("127.0.0.1", int(sys.argv[1])))\ns.listen()\nwhile not Path(sys.argv[3]).exists(): time.sleep(0.02)\n""",
        encoding="utf-8",
    )
    marker, old_exit, new_exit = (
        tmp_path / "delay",
        tmp_path / "old-exit",
        tmp_path / "new-exit",
    )
    old_config = ServiceConfig(
        run=f"{sys.executable} {script} {port} {marker} {old_exit}",
        ready=f"port:{port}",
    )
    new_config = ServiceConfig(
        run=f"{sys.executable} {script} {port} {marker} {new_exit}",
        ready=f"port:{port}",
    )
    manager = ProcessManager(tmp_path)
    manager.immediate_exit_window = 0
    crashes = MagicMock()
    old = manager.start_service("app", old_config, ready_timeout=3, on_crash=crashes)
    assert manager.wait_for_ready("app", timeout=3)
    old_pid = old.process.pid
    marker.touch()
    timer = threading.Timer(1.2, old_exit.touch)
    timer.start()
    try:
        started = time.monotonic()
        manager.overlap_service(
            "app", new_config, ready_timeout=3, exit_timeout=3, on_crash=crashes
        )
        assert time.monotonic() - started >= 0.4
        assert old.process.poll() is not None
        assert manager.get_pid("app") != old_pid
        assert manager.platform.is_port_owned_by_process_tree(
            port, manager.get_pid("app")
        )
        crashes.assert_not_called()
    finally:
        timer.cancel()
        manager.stop_service("app", timeout=1)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process fixture")
def test_failed_new_readiness_preserves_old_process(tmp_path: Path):
    manager = ProcessManager(tmp_path)
    manager.immediate_exit_window = 0
    config = ServiceConfig(run=f"{sys.executable} -c 'import time; time.sleep(30)'")
    old = manager.start_service("app", config)
    bad = ServiceConfig(
        run=f"{sys.executable} -c 'import time; time.sleep(30)'", ready="port:1"
    )
    try:
        with pytest.raises(RuntimeError, match="readiness"):
            manager.overlap_service("app", bad, ready_timeout=0.3, exit_timeout=1)
        assert manager.get_pid("app") == old.process.pid
        assert manager.is_running("app")
    finally:
        manager.stop_service("app", timeout=1)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux signal behavior")
def test_timeout_sends_sigterm_without_marking_old_as_crashed(tmp_path: Path, caplog):
    manager = ProcessManager(tmp_path)
    manager.immediate_exit_window = 0
    config = ServiceConfig(run=f"{sys.executable} -c 'import time; time.sleep(30)'")
    crashes = MagicMock()
    old = manager.start_service("app", config, on_crash=crashes)
    old_process = old.process
    try:
        with caplog.at_level("INFO"):
            manager.overlap_service(
                "app", config, ready_timeout=2, exit_timeout=0.1, on_crash=crashes
            )
        assert old_process.poll() is not None
        assert "method=SIGTERM" in caplog.text
        crashes.assert_not_called()
    finally:
        manager.stop_service("app", timeout=1)


@pytest.mark.parametrize(
    "previous,safe,running,expected",
    [
        (True, True, True, True),
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
    ],
)
def test_adapter_overlap_plan_and_dependent_filter(
    tmp_path, monkeypatch, previous, safe, running, expected
):
    from tests.test_service_restart_filter import _adapter_fixture
    from haniel.core.runner_deployment import RunnerDeploymentAdapter

    runner, repo, head = _adapter_fixture(tmp_path)
    runner.process_manager.is_running = MagicMock(
        side_effect=lambda name: running if name == "app" else True
    )
    adapter = RunnerDeploymentAdapter(
        runner, "app", ["app", "worker"], repo, head, target_head="new"
    )
    payload = manifest({"mode": "overlap"}).model_dump()
    payload["service_restart_filters"] = {"worker": {"ignore_paths": ["src/*"]}}
    adapter.manifest = ReleaseManifest.model_validate(payload)
    monkeypatch.setattr(
        "haniel.core.runner_deployment.read_file_at_commit",
        lambda *args: json.dumps(
            payload if previous else manifest().model_dump()
        ).encode(),
    )
    monkeypatch.setattr(
        "haniel.core.runner_deployment.get_changed_paths_between",
        lambda *args: ["src/app.py"],
    )
    candidates, overlap = adapter.plan_overlap(safe)
    assert ("app" in overlap) is expected
    assert (candidates is not None and "worker" in candidates.services) is expected
    if expected:
        adapter.apply_restart_skips(candidates.services)
        assert adapter._effective_quiesce_services() == []


def test_previous_manifest_missing_or_unreadable_falls_back(tmp_path, monkeypatch):
    from tests.test_service_restart_filter import _adapter_fixture
    from haniel.core.runner_deployment import RunnerDeploymentAdapter

    runner, repo, head = _adapter_fixture(tmp_path)
    runner.process_manager.is_running = MagicMock(return_value=True)
    adapter = RunnerDeploymentAdapter(
        runner, "app", ["app"], repo, "", target_head="new"
    )
    adapter.manifest = manifest({"mode": "overlap"})
    assert adapter.plan_overlap(True)[1] == frozenset()
    adapter.previous_head = head
    monkeypatch.setattr(
        "haniel.core.runner_deployment.read_file_at_commit",
        MagicMock(side_effect=ValueError("bad manifest")),
    )
    assert adapter.plan_overlap(True)[1] == frozenset()


@pytest.mark.parametrize(
    "pending,expected", [(0, True), (1, False), (None, False), (False, False)]
)
def test_preflight_controls_overlap_and_journal(tmp_path, pending, expected):
    from haniel.core.deployment import (
        CommandResult,
        DeploymentCallbacks,
        DeploymentCoordinator,
        DeploymentError,
    )
    from haniel.core.deployment_state import DeploymentStateStore
    from tests.test_service_restart_filter import _contract_preflight, _manifest_payload

    payload = _manifest_payload(contract=True)
    payload["service_restart_strategies"] = {"app": {"mode": "overlap"}}
    seen = []

    def plan(safe):
        seen.append(safe)
        return None, frozenset({"app"}) if safe else frozenset()

    def command(spec, environment):
        if spec.name == "preflight":
            data = _contract_preflight()
            if pending is not None:
                data["pending_count"] = pending
            return CommandResult(stdout="{}", json_data=data)

    callbacks = DeploymentCallbacks(
        build=lambda: None,
        stop=lambda: None,
        start_and_wait=lambda: None,
        rollback=lambda: None,
        prepare_roll_forward=lambda: None,
        plan_overlap=plan,
    )
    store = DeploymentStateStore(tmp_path)
    coordinator = DeploymentCoordinator(state_store=store, command_runner=command)
    try:
        coordinator.execute(
            repo_name="app",
            previous_head="old",
            target_head="new",
            manifest=ReleaseManifest.model_validate(payload),
            callbacks=callbacks,
            expected_operation="upgrade",
        )
    except DeploymentError:
        assert not expected
    assert seen == [expected]
    journal = store.read("app")
    assert journal.get("overlap_services", []) == (["app"] if expected else [])


def test_coordinator_applies_overlap_and_filtered_dependent(tmp_path, monkeypatch):
    from tests.test_service_restart_filter import _adapter_fixture, _mock_processes
    from haniel.core.deployment import DeploymentCoordinator
    from haniel.core.deployment_state import DeploymentStateStore
    from haniel.core.runner_deployment import RunnerDeploymentAdapter

    runner, repo, head = _adapter_fixture(tmp_path)
    events, _ = _mock_processes(runner, ["app", "worker"])
    adapter = RunnerDeploymentAdapter(
        runner, "app", ["app", "worker"], repo, head, target_head="new"
    )
    payload = manifest({"mode": "overlap"}).model_dump()
    payload["service_restart_filters"] = {"worker": {"ignore_paths": ["src/*"]}}
    adapter.manifest = ReleaseManifest.model_validate(payload)
    monkeypatch.setattr(
        "haniel.core.runner_deployment.read_file_at_commit",
        lambda *args: json.dumps(payload).encode(),
    )
    monkeypatch.setattr(
        "haniel.core.runner_deployment.get_changed_paths_between",
        lambda *args: ["src/app.py"],
    )
    adapter.build = lambda: None
    runner._blocked_start_dependencies = MagicMock(return_value=[])
    runner.process_manager.overlap_service = MagicMock(
        side_effect=lambda *args, **kwargs: events.append("overlap:app")
    )
    store = DeploymentStateStore(tmp_path / "journal")
    coordinator = DeploymentCoordinator(
        state_store=store, command_runner=lambda *args: None
    )
    result = coordinator.execute(
        repo_name="app",
        previous_head=head,
        target_head="new",
        manifest=adapter.manifest,
        callbacks=adapter.callbacks(),
    )
    assert result.status == "success"
    assert events == ["overlap:app"]
    journal = store.read("app")
    assert journal["overlap_services"] == ["app"]
    assert journal["restart_skipped_services"] == ["worker"]


def test_failed_overlap_is_excluded_from_rollback_stops_and_starts(tmp_path):
    from tests.test_service_restart_filter import _adapter_fixture, _mock_processes
    from haniel.core.runner_deployment import RunnerDeploymentAdapter

    runner, repo, head = _adapter_fixture(tmp_path)
    events, running = _mock_processes(runner, ["app"])
    adapter = RunnerDeploymentAdapter(
        runner, "app", ["app"], repo, head, target_head="new"
    )
    adapter.manifest = manifest({"mode": "overlap"})
    adapter.overlap_services = frozenset({"app"})
    runner._blocked_start_dependencies = MagicMock(return_value=[])
    runner.process_manager.overlap_service = MagicMock(
        side_effect=RuntimeError("readiness failed")
    )
    adapter.stop()
    with pytest.raises(RuntimeError, match="readiness failed"):
        adapter.start_and_wait({"app"})
    assert adapter.restart_skipped == frozenset({"app"})
    assert adapter._effective_quiesce_services() == []
    assert events == []
    assert running["app"]


def test_budget_includes_overlap_exit_wait():
    from haniel.core.deployment_budget import expected_deployment_budget

    config = {"app": ServiceConfig(run="app", repo="repo")}
    base = expected_deployment_budget(
        repo_name="repo", affected=["app"], services=config, manifest=manifest()
    )
    overlap = expected_deployment_budget(
        repo_name="repo",
        affected=["app"],
        services=config,
        manifest=manifest({"mode": "overlap", "exit_timeout_sec": 60}),
    )
    assert overlap.total_sec == base.total_sec + 60
    assert "overlap_exit=60s" in overlap.breakdown()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux signal behavior")
def test_old_process_ignoring_term_is_killed_after_shutdown_timeout(tmp_path, caplog):
    script = tmp_path / "ignore_term.py"
    marker = tmp_path / "ready"
    script.write_text(
        "import signal, sys, time\nfrom pathlib import Path\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nPath(sys.argv[1]).touch()\nwhile True: time.sleep(1)\n",
        encoding="utf-8",
    )
    manager = ProcessManager(tmp_path)
    manager.immediate_exit_window = 0
    old_config = ServiceConfig(
        run=f"{sys.executable} {script} {marker}", shutdown={"timeout": 1}
    )
    new_config = ServiceConfig(run=f"{sys.executable} -c 'import time; time.sleep(30)'")
    old = manager.start_service("app", old_config)
    old_process = old.process
    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists()
    try:
        with caplog.at_level("INFO"):
            manager.overlap_service(
                "app", new_config, ready_timeout=2, exit_timeout=0.1
            )
        assert old_process.poll() is not None
        assert "method=SIGKILL" in caplog.text
    finally:
        manager.stop_service("app", timeout=1)
