"""Log relays, handover state and self-update replacement contracts."""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import haniel
from haniel.config import HanielConfig, RepoConfig, ServiceConfig
from haniel.core import self_update_handover
from haniel.core.deployment import (
    CommandResult,
    DeploymentCallbacks,
    DeploymentCoordinator,
    contract_preflight_block_reason,
)
from haniel.core.deployment_state import DeploymentStateStore
from haniel.core.process import PidProcessHandle, ProcessManager
from haniel.core.release_manifest import ReleaseManifest
from haniel.core.restart_filter import RestartFilterCandidates
from haniel.core.self_update_handover import HandoverServiceState
from haniel.core.runner import ServiceRunner

SRC_DIR = Path(haniel.__file__).resolve().parents[1]
LOG_HEAD = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[(stdout|stderr)\] ")

linux_only = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux /proc identity and real process handover"
)

SERVICE_SCRIPT = """\
import socket, sys, time
from pathlib import Path

port = int(sys.argv[1])
stop = Path(sys.argv[2])
sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
sock.bind(("127.0.0.1", port))
sock.listen()
print("service started", flush=True)
print("service stderr line", file=sys.stderr, flush=True)
tick = 0
while not stop.exists():
    print(f"tick {tick}", flush=True)
    tick += 1
    time.sleep(0.05)
"""

PARENT_HANIEL_SCRIPT = """\
import os, sys
from pathlib import Path

from haniel.config import ServiceConfig
from haniel.core.process import PidProcessHandle, ProcessManager

config_dir = Path(sys.argv[1])
port = int(sys.argv[2])
service_script = Path(sys.argv[3])
stop = Path(sys.argv[4])
manager = ProcessManager(config_dir)
manager.immediate_exit_window = 0
config = ServiceConfig(
    run=f"{sys.executable} {service_script} {port} {stop}",
    ready=f"port:{port}",
)
managed = manager.start_service(
    "app", config, ready_timeout=10, log_relay=True
)
if not manager.wait_for_ready("app", timeout=10):
    raise SystemExit("service did not become ready")
print(managed.process.pid, flush=True)
os._exit(0)
"""


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _relay_pids(log_path: Path) -> list[int]:
    marker = str(log_path)
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        command = raw.replace(b"\0", b" ").decode("utf-8", errors="replace")
        if "haniel.core.log_sink" in command and marker in command:
            pids.append(int(entry.name))
    return pids


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _cleanup_service(pid: int, log_path: Path, stop: Path) -> None:
    stop.touch()
    _kill(pid)
    for relay_pid in _relay_pids(log_path):
        _kill(relay_pid)
    _wait_until(lambda: not _is_alive(pid) and not _relay_pids(log_path), timeout=5)


def _write_scripts(tmp_path: Path, port: int) -> tuple[Path, Path]:
    service_script = tmp_path / "service.py"
    service_script.write_text(SERVICE_SCRIPT, encoding="utf-8")
    parent_script = tmp_path / "parent_haniel.py"
    parent_script.write_text(PARENT_HANIEL_SCRIPT, encoding="utf-8")
    return service_script, parent_script


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(SRC_DIR) if not existing else f"{SRC_DIR}{os.pathsep}{existing}"
    )
    return env


def _manifest(mode: str = "overlap") -> ReleaseManifest:
    return ReleaseManifest.model_validate(
        {
            "schema_version": "haniel.release.v1",
            "release_id": "one",
            "post_start_verify": [{"name": "verify", "command": "true"}],
            "recovery": {
                "strategy": "rollback",
                "command": {"name": "restore", "command": "true"},
            },
            "service_restart_strategies": {"app": {"mode": mode}},
        }
    )


def _runner(tmp_path: Path, app_command: str | None = None) -> ServiceRunner:
    (tmp_path / "repo").mkdir()
    sleep_script = tmp_path / "sleep_service.py"
    sleep_script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    command = f'"{sys.executable}" "{sleep_script}"'
    config = HanielConfig(
        repos={
            "repo": RepoConfig(
                url="git@github.com:test/repo.git",
                path="./repo",
                release_manifest="release.json",
            )
        },
        services={
            "app": ServiceConfig(run=app_command or command, repo="repo"),
            "worker": ServiceConfig(run=command),
        },
    )
    return ServiceRunner(config, config_dir=tmp_path)


def test_handover_state_round_trip_and_clear(tmp_path: Path):
    entry = HandoverServiceState(
        name="app", pid=1234, repo="repo", head="abc123", exit_timeout_sec=60
    )
    path = self_update_handover.write(tmp_path, [entry])
    assert path == tmp_path / ".local" / "self_update_handover.json"
    assert sorted(item.name for item in path.parent.iterdir()) == [path.name]

    assert self_update_handover.read_and_clear(tmp_path) == [entry]
    assert not path.exists()
    assert self_update_handover.read_and_clear(tmp_path) == []


def test_handover_state_rejects_invalid_and_clears(tmp_path: Path, caplog):
    path = tmp_path / ".local" / "self_update_handover.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 1, "services": [{"name": "app"}]}', encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert self_update_handover.read_and_clear(tmp_path) == []
    assert "invalid self-update handover" in caplog.text
    assert not path.exists()


@pytest.mark.parametrize(
    "operation,payload,expected",
    [
        (
            "fresh_install",
            {"pending_count": 0},
            "contract operation is not an upgrade "
            "(operation=fresh_install, pending_count=0)",
        ),
        (
            "upgrade",
            {},
            "contract preflight result has no pending_count "
            "(operation=upgrade, pending_count=missing)",
        ),
        (
            "upgrade",
            {"pending_count": True},
            "contract preflight pending_count is not an integer "
            "(operation=upgrade, pending_count=True)",
        ),
        (
            "upgrade",
            {"pending_count": 2},
            "contract preflight pending_count is not zero "
            "(operation=upgrade, pending_count=2)",
        ),
    ],
)
def test_contract_preflight_block_reason_names_cause_and_value(
    operation, payload, expected
):
    result = CommandResult(stdout="{}", json_data=payload)
    assert (
        contract_preflight_block_reason(
            contract_mode=True, operation=operation, preflight=result
        )
        == expected
    )


def test_contract_preflight_block_reason_allows_safe_preflight():
    result = CommandResult(stdout="{}", json_data={"pending_count": 0})
    assert (
        contract_preflight_block_reason(
            contract_mode=True, operation="upgrade", preflight=result
        )
        is None
    )
    assert (
        contract_preflight_block_reason(
            contract_mode=False, operation="fresh_install", preflight=None
        )
        is None
    )


def test_blocked_restart_skips_log_the_preflight_reason(tmp_path: Path, caplog):
    coordinator = DeploymentCoordinator(
        state_store=DeploymentStateStore(tmp_path),
        command_runner=lambda spec, environment: None,
    )
    hit = RestartFilterCandidates(services=frozenset({"app"}), changed_path_count=1)
    callbacks = DeploymentCallbacks(
        build=lambda: None,
        stop=lambda: None,
        start_and_wait=lambda: None,
        rollback=lambda: None,
        prepare_roll_forward=lambda: None,
        restart_candidates=hit,
    )
    reason = "contract preflight pending_count is not zero (operation=upgrade, pending_count=1)"
    with caplog.at_level("INFO"):
        confirmed = coordinator._confirm_restart_skips(
            repo_name="repo",
            target_head="abcdef1234567890",
            callbacks=callbacks,
            block_reason=reason,
        )
    assert confirmed == frozenset()
    assert reason in caplog.text
    assert "were not applied" in caplog.text


@linux_only
def test_relay_backed_service_survives_parent_exit_and_keeps_logging(tmp_path: Path):
    port = _free_port()
    service_script, parent_script = _write_scripts(tmp_path, port)
    config_dir = tmp_path / "haniel"
    stop = tmp_path / "stop"
    log_path = config_dir / "logs" / "app.log"
    stderr_path = tmp_path / "parent.err"
    with stderr_path.open("wb") as child_stderr:
        child = subprocess.run(
            [
                sys.executable,
                str(parent_script),
                str(config_dir),
                str(port),
                str(service_script),
                str(stop),
            ],
            stdout=subprocess.PIPE,
            stderr=child_stderr,
            text=True,
            timeout=60,
            env=_subprocess_env(),
        )
    assert child.returncode == 0, stderr_path.read_text(
        encoding="utf-8", errors="replace"
    )
    pid = int(child.stdout.strip().splitlines()[-1])
    try:
        assert _is_alive(pid)
        assert _wait_until(lambda: log_path.exists(), timeout=5)
        before = log_path.read_text(encoding="utf-8").splitlines()
        assert before
        assert _wait_until(
            lambda: (
                len(log_path.read_text(encoding="utf-8").splitlines()) > len(before)
            ),
            timeout=5,
        )
        assert _wait_until(
            lambda: any(
                "[stdout] tick " in line
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ),
            timeout=5,
        )
        assert _wait_until(
            lambda: any(
                "[stderr] service stderr line" in line
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ),
            timeout=5,
        )
        lines = log_path.read_text(encoding="utf-8").splitlines()
        relayed = [line for line in lines if line and not line.startswith("=== ")]
        assert relayed
        assert all(LOG_HEAD.match(line) for line in relayed)
    finally:
        _cleanup_service(pid, log_path, stop)


@linux_only
def test_new_process_manager_adopts_handover_state(tmp_path: Path, caplog):
    port = _free_port()
    service_script, _ = _write_scripts(tmp_path, port)
    stop = tmp_path / "stop"
    config = ServiceConfig(
        run=f"{sys.executable} {service_script} {port} {stop}",
        ready=f"port:{port}",
    )
    old_manager = ProcessManager(tmp_path / "old")
    old_manager.immediate_exit_window = 0
    managed = old_manager.start_service("app", config, ready_timeout=10, log_relay=True)
    assert old_manager.wait_for_ready("app", timeout=10)
    old_pid = managed.process.pid
    entry = HandoverServiceState(
        name="app", pid=old_pid, repo="repo", head="head1", exit_timeout_sec=1
    )
    path = self_update_handover.write(tmp_path, [entry])
    assert path.exists()
    assert self_update_handover.read_and_clear(tmp_path) == [entry]
    assert not path.exists()

    new_manager = ProcessManager(tmp_path / "new")
    try:
        with caplog.at_level("INFO"):
            adopted = new_manager.adopt_handover_service(
                "app",
                config,
                previous_pid=entry.pid,
                exit_timeout=entry.exit_timeout_sec,
                ready_timeout=10,
                log_relay=True,
            )
        assert adopted is True
        assert new_manager.is_running("app")
        assert new_manager.get_pid("app") != old_pid
        assert _wait_until(lambda: not _is_alive(old_pid), timeout=5)
        assert "Self-update handover for app" in caplog.text
        assert re.search(r"method=(self|term|kill)", caplog.text)
    finally:
        new_manager.stop_service("app", timeout=1)
        for manager_dir in ("old", "new"):
            _cleanup_service(old_pid, tmp_path / manager_dir / "logs" / "app.log", stop)


@linux_only
def test_adoption_falls_back_when_previous_pid_is_gone(tmp_path: Path):
    manager = ProcessManager(tmp_path)
    manager.immediate_exit_window = 0
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    config = ServiceConfig(run=f"{sys.executable} -c 'import time; time.sleep(30)'")
    assert (
        manager.adopt_handover_service(
            "app",
            config,
            previous_pid=dead.pid,
            exit_timeout=0.2,
            ready_timeout=1,
        )
        is False
    )
    assert not manager.is_running("app")


@linux_only
def test_adoption_fallback_stops_replacement_and_old_pid(tmp_path: Path, caplog):
    port = _free_port()
    service_script, _ = _write_scripts(tmp_path, port)
    stop = tmp_path / "stop"
    good = ServiceConfig(
        run=f"{sys.executable} {service_script} {port} {stop}",
        ready=f"port:{port}",
    )
    manager = ProcessManager(tmp_path)
    manager.immediate_exit_window = 0
    managed = manager.start_service("app", good, ready_timeout=10, log_relay=True)
    assert manager.wait_for_ready("app", timeout=10)
    old_pid = managed.process.pid
    bad = good.model_copy(update={"ready": "port:1"})
    try:
        with caplog.at_level("INFO"):
            adopted = manager.adopt_handover_service(
                "app",
                bad,
                previous_pid=old_pid,
                exit_timeout=0.5,
                ready_timeout=0.3,
                log_relay=True,
            )
        assert adopted is False
        assert not manager.is_running("app")
        assert _wait_until(lambda: not _is_alive(old_pid), timeout=5)
        assert "replacement failed readiness; starting normally" in caplog.text
    finally:
        _cleanup_service(old_pid, tmp_path / "logs" / "app.log", stop)


def test_runner_skips_handover_when_repository_head_moved(
    tmp_path: Path, monkeypatch, caplog
):
    runner = _runner(tmp_path)
    entry = HandoverServiceState(
        name="app", pid=os.getpid(), repo="repo", head="oldhead", exit_timeout_sec=5
    )
    monkeypatch.setattr("haniel.core.runner.get_head", lambda path: "newhead")
    with caplog.at_level("INFO"):
        adopted = runner._adopt_self_update_handover(
            "app", runner._snapshot_config_state().enabled_services["app"], entry
        )
    assert adopted is False
    assert "Self-update handover for app skipped" in caplog.text
    assert "HEAD moved" in caplog.text


def test_runner_handover_mismatch_checks_identity(tmp_path: Path, monkeypatch):
    runner = _runner(tmp_path)
    config = runner._snapshot_config_state().enabled_services["app"]
    entry = HandoverServiceState(
        name="app", pid=4321, repo="repo", head="headsha", exit_timeout_sec=5
    )
    monkeypatch.setattr("haniel.core.runner.get_head", lambda path: "headsha")
    monkeypatch.setattr(
        runner, "_current_release_manifest", lambda runtime: _manifest()
    )
    platform = runner.process_manager.platform
    monkeypatch.setattr(platform, "is_pid_running", lambda pid: True)
    monkeypatch.setattr(platform, "is_pid_zombie", lambda pid: False)
    monkeypatch.setattr("haniel.core.runner.pid_matches_service", lambda *args: True)
    assert runner._handover_mismatch("app", config, entry) is None

    monkeypatch.setattr(platform, "is_pid_running", lambda pid: False)
    assert "is gone" in runner._handover_mismatch("app", config, entry)


def test_runner_stop_services_keeps_handover_services(tmp_path: Path):
    runner = _runner(tmp_path)
    config = runner._snapshot_config_state().enabled_services
    runner.process_manager.start_service("app", config["app"])
    runner.process_manager.start_service("worker", config["worker"])
    try:
        assert runner.stop_services(skip=frozenset({"app"})) is True
        assert runner.process_manager.is_running("app")
        assert not runner.process_manager.is_running("worker")
    finally:
        runner.stop_services()


def test_arm_handover_writes_relay_backed_services(tmp_path: Path, monkeypatch):
    runner = _runner(tmp_path)
    config = runner._snapshot_config_state().enabled_services["app"]
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    runner.process_manager._processes["app"] = SimpleNamespace(
        config=config, process=process, relay_processes=[MagicMock()]
    )
    monkeypatch.setattr("haniel.core.runner.get_head", lambda path: "headsha")
    monkeypatch.setattr(
        runner, "_current_release_manifest", lambda runtime: _manifest()
    )
    state_path = tmp_path / ".local" / "self_update_handover.json"
    try:
        kept = runner._arm_self_update_handover()
        assert kept == frozenset({"app"})
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        assert payload["services"] == [
            {
                "name": "app",
                "pid": process.pid,
                "repo": "repo",
                "head": "headsha",
                "exit_timeout_sec": 60,
            }
        ]
        runner._discard_self_update_handover()
        assert not state_path.exists()
    finally:
        process.kill()
        process.wait()


def test_manifest_accepts_adopt_and_rejects_unknown_modes():
    assert _manifest("adopt").service_restart_strategies["app"].mode == "adopt"
    with pytest.raises(ValueError):
        _manifest("stop")


def test_handover_state_records_adopt_mode_and_defaults_to_overlap(tmp_path: Path):
    adopt = HandoverServiceState(
        name="app",
        pid=1234,
        repo="repo",
        head="abc123",
        exit_timeout_sec=60,
        mode="adopt",
    )
    path = self_update_handover.write(tmp_path, [adopt])
    assert json.loads(path.read_text(encoding="utf-8"))["services"][0]["mode"] == (
        "adopt"
    )
    assert self_update_handover.read_and_clear(tmp_path) == [adopt]

    path.write_text(
        json.dumps(
            {
                "version": 1,
                "services": [
                    {
                        "name": "app",
                        "pid": 1,
                        "repo": "repo",
                        "head": "h",
                        "exit_timeout_sec": 5,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert self_update_handover.read_and_clear(tmp_path)[0].mode == "overlap"

    path.write_text(
        '{"version": 1, "services": [{"name": "app", "pid": 1, "repo": "r", '
        '"head": "h", "exit_timeout_sec": 5, "mode": "other"}]}',
        encoding="utf-8",
    )
    assert self_update_handover.read_and_clear(tmp_path) == []


def test_adopt_declaration_makes_the_service_relay_backed(tmp_path: Path, monkeypatch):
    runner = _runner(tmp_path)
    config = runner._snapshot_config_state().enabled_services["app"]
    for mode in ("overlap", "adopt"):
        monkeypatch.setattr(
            runner, "_current_release_manifest", lambda runtime, m=mode: _manifest(m)
        )
        assert runner._is_log_relay_target("app", config) is True


def test_arm_handover_records_adopt_mode_for_adopted_services(
    tmp_path: Path, monkeypatch
):
    runner = _runner(tmp_path)
    config = runner._snapshot_config_state().enabled_services["app"]
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    runner.process_manager._processes["app"] = SimpleNamespace(
        config=config, process=process, relay_processes=[], relay_backed=True
    )
    monkeypatch.setattr("haniel.core.runner.get_head", lambda path: "headsha")
    monkeypatch.setattr(
        runner, "_current_release_manifest", lambda runtime: _manifest("adopt")
    )
    state_path = tmp_path / ".local" / "self_update_handover.json"
    try:
        assert runner._arm_self_update_handover() == frozenset({"app"})
        service = json.loads(state_path.read_text(encoding="utf-8"))["services"][0]
        assert service["mode"] == "adopt"
        assert service["pid"] == process.pid
        assert runner.stop_services(skip=frozenset({"app"})) is True
        assert process.poll() is None
    finally:
        process.kill()
        process.wait()


def test_handover_mismatch_rejects_a_changed_mode(tmp_path: Path, monkeypatch):
    runner = _runner(tmp_path)
    config = runner._snapshot_config_state().enabled_services["app"]
    entry = HandoverServiceState(
        name="app",
        pid=4321,
        repo="repo",
        head="headsha",
        exit_timeout_sec=5,
        mode="adopt",
    )
    monkeypatch.setattr("haniel.core.runner.get_head", lambda path: "headsha")
    monkeypatch.setattr(
        runner, "_current_release_manifest", lambda runtime: _manifest("overlap")
    )
    assert "mode changed from adopt to overlap" in (
        runner._handover_mismatch("app", config, entry)
    )


def test_adopt_mismatch_retires_the_inherited_pid(tmp_path: Path, monkeypatch):
    runner = _runner(tmp_path)
    config = runner._snapshot_config_state().enabled_services["app"]
    entry = HandoverServiceState(
        name="app",
        pid=4321,
        repo="repo",
        head="oldhead",
        exit_timeout_sec=5,
        mode="adopt",
    )
    monkeypatch.setattr("haniel.core.runner.get_head", lambda path: "newhead")
    retired: list[int] = []
    monkeypatch.setattr(
        runner.process_manager,
        "retire_inherited_pid",
        lambda name, cfg, pid: retired.append(pid),
    )
    assert runner._adopt_self_update_handover("app", config, entry) is False
    assert retired == [4321]


def test_pid_handle_monitor_interval_follows_the_platform():
    if os.name == "nt":
        assert PidProcessHandle.MONITOR_INTERVAL >= 10
    else:
        assert PidProcessHandle.MONITOR_INTERVAL <= 2.5


def _start_orphan_service(tmp_path: Path, port: int) -> tuple[int, ServiceConfig, Path]:
    """Start a relay-backed service whose Haniel parent has already exited."""
    service_script, parent_script = _write_scripts(tmp_path, port)
    stop = tmp_path / "stop"
    config = ServiceConfig(
        run=f"{sys.executable} {service_script} {port} {stop}",
        ready=f"port:{port}",
    )
    stderr_path = tmp_path / "parent.err"
    with stderr_path.open("wb") as child_stderr:
        child = subprocess.run(
            [
                sys.executable,
                str(parent_script),
                str(tmp_path / "old"),
                str(port),
                str(service_script),
                str(stop),
            ],
            stdout=subprocess.PIPE,
            stderr=child_stderr,
            text=True,
            timeout=60,
            env=_subprocess_env(),
        )
    assert child.returncode == 0, stderr_path.read_text(
        encoding="utf-8", errors="replace"
    )
    return int(child.stdout.strip().splitlines()[-1]), config, stop


@linux_only
def test_adopt_keeps_the_same_pid_without_starting_a_process(tmp_path: Path, caplog):
    pid, config, stop = _start_orphan_service(tmp_path, _free_port())
    manager = ProcessManager(tmp_path / "new")
    ready: list[str] = []
    try:
        with caplog.at_level("INFO"):
            adopted = manager.adopt_inherited_pid(
                "app", config, pid=pid, on_ready=lambda: ready.append("app")
            )
        assert adopted is True
        assert manager.is_running("app")
        assert manager.get_pid("app") == pid
        assert ready == ["app"]
        assert f"Adopted app (pid {pid}) without restart" in caplog.text
        assert [name for name, _ in manager.relay_backed_processes()] == ["app"]
        assert _is_alive(pid)
    finally:
        _cleanup_service(pid, tmp_path / "old" / "logs" / "app.log", stop)


@linux_only
def test_adopted_pid_that_disappears_is_handled_as_a_crash(
    tmp_path: Path, monkeypatch, caplog
):
    monkeypatch.setattr(PidProcessHandle, "MONITOR_INTERVAL", 0.05)
    pid, config, stop = _start_orphan_service(tmp_path, _free_port())
    manager = ProcessManager(tmp_path / "new")
    crashes: list[int | None] = []
    try:
        assert manager.adopt_inherited_pid(
            "app", config, pid=pid, on_crash=crashes.append
        )
        with caplog.at_level("WARNING"):
            os.kill(pid, signal.SIGKILL)
            assert _wait_until(lambda: crashes == [None], timeout=5)
        assert not manager.is_running("app")
        assert "exit code is unknown" in caplog.text
    finally:
        _cleanup_service(pid, tmp_path / "old" / "logs" / "app.log", stop)


@linux_only
def test_stopping_an_adopted_service_stops_its_pid(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(PidProcessHandle, "MONITOR_INTERVAL", 0.05)
    pid, config, stop = _start_orphan_service(tmp_path, _free_port())
    manager = ProcessManager(tmp_path / "new")
    crashes: list[int | None] = []
    try:
        assert manager.adopt_inherited_pid(
            "app", config, pid=pid, on_crash=crashes.append
        )
        assert manager.stop_service("app", timeout=5) is True
        assert _wait_until(lambda: not _is_alive(pid), timeout=5)
        assert not manager.is_running("app")
        time.sleep(0.3)
        assert crashes == []
    finally:
        _cleanup_service(pid, tmp_path / "old" / "logs" / "app.log", stop)


@linux_only
def test_adopt_falls_back_when_the_pid_is_gone(tmp_path: Path):
    manager = ProcessManager(tmp_path)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    config = ServiceConfig(run=f"{sys.executable} -c 'import time; time.sleep(30)'")
    assert manager.adopt_inherited_pid("app", config, pid=dead.pid) is False
    assert not manager.is_running("app")


@linux_only
def test_adopt_retires_the_pid_when_the_ready_check_fails(tmp_path: Path, caplog):
    pid, config, stop = _start_orphan_service(tmp_path, _free_port())
    manager = ProcessManager(tmp_path / "new")
    bad = config.model_copy(update={"ready": "port:1"})
    try:
        with caplog.at_level("WARNING"):
            assert manager.adopt_inherited_pid("app", bad, pid=pid) is False
        assert not manager.is_running("app")
        assert _wait_until(lambda: not _is_alive(pid), timeout=10)
        assert "failed the ready check" in caplog.text
    finally:
        _cleanup_service(pid, tmp_path / "old" / "logs" / "app.log", stop)


@linux_only
def test_runner_adopts_an_adopt_entry_without_a_new_process(
    tmp_path: Path, monkeypatch
):
    (tmp_path / "svc").mkdir()
    pid, config, stop = _start_orphan_service(tmp_path / "svc", _free_port())
    runner = _runner(tmp_path, app_command=config.run)
    app_config = (
        runner._snapshot_config_state()
        .enabled_services["app"]
        .model_copy(update={"ready": config.ready})
    )
    entry = HandoverServiceState(
        name="app",
        pid=pid,
        repo="repo",
        head="headsha",
        exit_timeout_sec=60,
        mode="adopt",
    )
    monkeypatch.setattr("haniel.core.runner.get_head", lambda path: "headsha")
    monkeypatch.setattr(
        runner, "_current_release_manifest", lambda runtime: _manifest("adopt")
    )
    try:
        assert runner._adopt_self_update_handover("app", app_config, entry) is True
        assert runner.process_manager.get_pid("app") == pid
        runner.stop_services()
        assert _wait_until(lambda: not _is_alive(pid), timeout=5)
    finally:
        _cleanup_service(pid, tmp_path / "svc" / "old" / "logs" / "app.log", stop)
