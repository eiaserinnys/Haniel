"""Readiness parsing, lifecycle, and runtime apply contracts."""

from __future__ import annotations

import importlib
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from haniel.config import HanielConfig, RepoConfig, ServiceConfig
from haniel.config.validators import require_valid_config, validate_config
from haniel.core.health import ServiceState
from haniel.core.deployment_state import DeploymentStateStore
from haniel.core.logs import StreamReader
from haniel.core.process import ManagedProcess, ProcessManager
from haniel.core.runner import ServiceRunner

CHILD = Path(__file__).parent / "fixtures" / "readiness_child.py"
MARKER = "READY-MARKER"


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = __import__("time").monotonic() + timeout
    while __import__("time").monotonic() < deadline:
        if predicate():
            return True
        threading.Event().wait(0.01)
    return bool(predicate())


def _process_is_running(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        kernel32.CloseHandle(handle)


def _process_resource_count() -> int:
    if os.name != "nt":
        return len(list(Path("/proc/self/fd").iterdir()))
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL
    count = wintypes.DWORD()
    if not kernel32.GetProcessHandleCount(
        kernel32.GetCurrentProcess(), ctypes.byref(count)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(count.value)


def _status(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _child_command(
    tmp_path: Path,
    mode: str,
    *,
    release: Path | None = None,
    exit_after_marker: bool = False,
    grandchild: bool = False,
    escaped_grandchild: bool = False,
) -> tuple[str, Path]:
    status = tmp_path / f"{mode}-{len(list(tmp_path.glob('*.json')))}.json"
    command = [
        sys.executable,
        str(CHILD),
        "--mode",
        mode,
        "--status",
        str(status),
        "--marker",
        MARKER,
    ]
    if release is not None:
        command.extend(("--release", str(release)))
    if exit_after_marker:
        command.append("--exit-after-marker")
    if grandchild:
        command.append("--grandchild")
    if escaped_grandchild:
        command.append("--escaped-grandchild")
    if sys.platform == "win32":
        return subprocess.list2cmdline(command), status
    return shlex.join(command), status


def _manager(tmp_path: Path) -> ProcessManager:
    return ProcessManager(config_dir=tmp_path, log_dir=tmp_path / "logs")


def _config(run: str, ready: str | None) -> ServiceConfig:
    return ServiceConfig(run=run, ready=ready)


def _dump_config(path: Path, config: HanielConfig) -> bytes:
    rendered = yaml.safe_dump(
        config.model_dump(mode="json", by_alias=True, exclude_none=True),
        sort_keys=False,
        allow_unicode=True,
    ).encode("utf-8")
    path.write_bytes(rendered)
    return rendered


def _runner_with_manager(
    tmp_path: Path,
    manager: ProcessManager,
    name: str,
    config: ServiceConfig,
) -> ServiceRunner:
    runner = ServiceRunner(
        HanielConfig(services={name: config}),
        config_dir=tmp_path,
        config_path=tmp_path / "haniel.yaml",
    )
    runner.process_manager = manager
    runner.health_manager = manager.health_manager
    return runner


def test_enabled_service_without_ready_is_warning_not_global_rejection(
    tmp_path: Path,
) -> None:
    """R1: migration warnings do not reject an otherwise valid config."""
    config_path = tmp_path / "haniel.yaml"
    migrating = HanielConfig(
        repos={
            "app": RepoConfig(
                url="https://example.invalid/app.git",
                path="./app",
                auto_apply=False,
            )
        },
        services={"app": ServiceConfig(run="python app.py", repo="app")},
    )
    _dump_config(config_path, migrating)
    findings = validate_config(migrating)
    warning = next(
        error
        for error in findings
        if error.location == "services.app.ready"
        and getattr(error, "code", None) == "READINESS_REQUIRED"
    )
    assert warning.severity == "warning"
    assert require_valid_config(migrating).error_count == 0


def test_release_activation_still_rejects_invalid_provided_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    activation = importlib.import_module("haniel.config.release_activation")
    semantic_error = getattr(activation, "ReleaseActivationSemanticError", RuntimeError)
    identity_error = getattr(activation, "ReleaseActivationIdentityDrift", RuntimeError)

    config_path = tmp_path / "haniel.yaml"
    invalid = HanielConfig(
        repos={
            "app": RepoConfig(
                url="https://example.invalid/app.git",
                path="./app",
                auto_apply=False,
            )
        },
        services={
            "app": ServiceConfig(run="python app.py", repo="app", ready="port:0")
        },
    )
    original = _dump_config(config_path, invalid)
    before_entries = {entry.name for entry in tmp_path.iterdir()}
    findings = validate_config(invalid)
    assert any(
        error.location == "services.app.ready"
        and getattr(error, "code", None) == "READINESS_PORT_INVALID"
        and error.severity == "error"
        for error in findings
    ), "validator-direct"

    with pytest.raises(semantic_error) as planned:
        activation.plan_release_manifest_activation(
            config_path, "app", activation.DEFAULT_RELEASE_MANIFEST
        )
    assert getattr(planned.value, "code", None) == "CONFIG_SEMANTIC_INVALID"
    assert config_path.read_bytes() == original
    assert {entry.name for entry in tmp_path.iterdir()} == before_entries
    assert list(tmp_path.glob("*.bak")) == []

    valid = invalid.model_copy(deep=True)
    valid.services["app"] = valid.services["app"].model_copy(
        update={"ready": "delay:0.01"}
    )
    valid_bytes = _dump_config(config_path, valid)
    monkeypatch.setattr(
        activation,
        "_remote_release_identity",
        lambda *_args: ("remote-head", "manifest-digest"),
    )
    plan = activation.plan_release_manifest_activation(
        config_path, "app", activation.DEFAULT_RELEASE_MANIFEST
    )
    drifted = valid.model_copy(deep=True)
    drifted.poll_interval += 1
    drifted_bytes = _dump_config(config_path, drifted)
    callback_count = 0
    reload_count = 0

    with pytest.raises(identity_error) as drift:
        activation.activate_release_manifest(config_path, plan=plan)
    assert getattr(drift.value, "code", None) == "CONFIG_IDENTITY_DRIFT"
    assert config_path.read_bytes() == drifted_bytes
    assert config_path.read_bytes() != valid_bytes
    assert list(tmp_path.glob("*.bak")) == []
    assert callback_count == reload_count == 0


def test_unknown_or_semantically_invalid_ready_is_validation_error() -> None:
    """R2: the canonical parser rejects every malformed readiness shape."""
    readiness = importlib.import_module("haniel.config.readiness")
    parse = readiness.parse_ready_condition

    for value in (
        "",
        "unknown:value",
        "port:0",
        "port:65536",
        "port:not-a-port",
        "delay:0",
        "delay:-1",
        "delay:nan",
        "delay:inf",
        "log:",
        "log:(",
        "http:",
    ):
        with pytest.raises(ValueError, match="READINESS_"):
            parse(value)

    assert parse("port:1").port == 1
    assert parse("port:65535").port == 65535
    assert parse("delay:0.01").delay == pytest.approx(0.01)
    assert parse("log:READY").pattern.pattern == "READY"
    assert parse("http:localhost:8080/health").endpoint.endswith("/health")


def test_missing_ready_runs_but_remains_never_ready(tmp_path: Path) -> None:
    command, status = _child_command(tmp_path, "hold")
    manager = _manager(tmp_path)
    config = _config(command, None)
    managed = manager.start_service("migrating", config, ready_timeout=0.05)
    runner = _runner_with_manager(tmp_path, manager, "migrating", config)
    try:
        assert _wait(status.exists)
        assert manager.is_running("migrating")
        assert manager.wait_for_ready("migrating", timeout=0.05) is False
        assert managed.readiness_state.value == "unconfigured"
        assert manager.health_manager.get_health("migrating").state is (
            ServiceState.RUNNING
        )
        assert runner._collect_services_info()[0]["ready"] is False
    finally:
        manager.stop_service("migrating", force=True)


def test_invalid_provided_ready_never_spawns_or_signals_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3: malformed provided readiness fails before every observable side effect."""
    validators = importlib.import_module("haniel.config.validators")
    semantic_error = getattr(validators, "ConfigSemanticError", RuntimeError)
    manager = _manager(tmp_path)
    spawn = MagicMock(side_effect=AssertionError("spawn side effect"))
    monkeypatch.setattr(manager, "_spawn_process", spawn)

    callbacks: list[str] = []
    with pytest.raises(semantic_error):
        manager.start_service(
            "invalid",
            _config("python app.py", "port:0"),
            on_ready=lambda: callbacks.append("ready"),
        )
    assert callbacks == []
    assert manager.get_pid("invalid") is None
    assert not (tmp_path / "logs" / "invalid.log").exists()
    spawn.assert_not_called()


def test_immediate_log_marker_from_real_child_is_not_missed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: callback arm happens before a reader consumes the first marker."""
    original_start = StreamReader.start

    def start_after_consumption_barrier(reader: StreamReader) -> None:
        original_start(reader)
        if reader.source == "stdout":
            assert _wait(
                lambda: any(
                    MARKER in line for line in reader.log_capture.get_recent_lines()
                )
            )

    monkeypatch.setattr(StreamReader, "start", start_after_consumption_barrier)
    manager = _manager(tmp_path)
    try:
        for index in range(50):
            command, _ = _child_command(tmp_path, "immediate-log")
            name = f"immediate-{index}"
            manager.start_service(
                name,
                _config(command, f"log:{MARKER}"),
                ready_timeout=1,
            )
            assert manager.wait_for_ready(name, timeout=1), index
            assert manager.stop_service(name, force=True)
    finally:
        manager.stop_all()


def test_stale_generation_callback_cannot_ready_replacement(tmp_path: Path) -> None:
    """R5: only the currently installed process generation may commit ready."""
    manager = _manager(tmp_path)
    process = MagicMock(pid=100)
    process.poll.return_value = None
    old = ManagedProcess(
        name="app",
        config=_config("python app.py", f"log:{MARKER}"),
        process=process,
        ready_event=threading.Event(),
        generation=1,
    )
    replacement = ManagedProcess(
        name="app",
        config=_config("python app.py", f"log:{MARKER}"),
        process=process,
        ready_event=threading.Event(),
        generation=2,
    )
    manager._processes["app"] = replacement

    assert manager._commit_ready(old) is False
    assert not replacement.ready_event.is_set()
    assert manager.health_manager.get_health("app").state is not ServiceState.READY


def test_timeout_marker_and_generation_races_never_end_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6: timeout, deadline, and replacement are terminal generation barriers."""
    release = tmp_path / "release.marker"
    command, status = _child_command(tmp_path, "tcp-before-log", release=release)
    manager = _manager(tmp_path)
    managed = manager.start_service(
        "app",
        _config(command, f"log:{MARKER}"),
        ready_timeout=0.05,
    )
    try:
        assert _wait(status.exists)
        assert manager.wait_for_ready("app", timeout=0.2) is False
        assert managed.readiness_state.value == "timed_out", "timeout-late-marker"
        assert managed.ready_callback_handle is None
        assert managed.log_capture is not None
        assert managed.log_capture.callback_count == 0
        assert managed.ready_monitor is None or not managed.ready_monitor.is_alive()

        release.touch()
        assert _wait(lambda: bool(_status(status).get("marker_emitted")))
        assert manager.health_manager.get_health("app").state is not ServiceState.READY
        assert not managed.ready_event.is_set()

        process = MagicMock(pid=101)
        process.poll.return_value = None
        before_deadline = ManagedProcess(
            name="deadline",
            config=_config("python app.py", f"log:{MARKER}"),
            process=process,
            ready_event=threading.Event(),
            generation=10,
        )
        manager._processes["deadline"] = before_deadline
        assert manager._commit_ready(
            before_deadline, observed_at=10.0, deadline=10.0
        ), "marker-deadline-barrier"

        timed_out = ManagedProcess(
            name="replace",
            config=_config("python app.py", f"log:{MARKER}"),
            process=process,
            ready_event=threading.Event(),
            generation=11,
        )
        replacement = ManagedProcess(
            name="replace",
            config=_config("python app.py", f"log:{MARKER}"),
            process=process,
            ready_event=threading.Event(),
            generation=12,
        )
        timed_out.readiness_state = importlib.import_module(
            "haniel.core.process"
        ).ReadinessState.TIMED_OUT
        manager._processes["replace"] = replacement
        assert manager._commit_ready(timed_out) is False, "timeout-generation-replace"
        assert not replacement.ready_event.is_set()

        # A READY commit and process exit must share one lifecycle order.  If
        # health publication happens after releasing the generation lock, a
        # delayed record_ready() can overwrite the later CRASHED state.
        health_race = ManagedProcess(
            name="health-race",
            config=_config("python app.py", f"log:{MARKER}"),
            process=process,
            ready_event=threading.Event(),
            generation=13,
        )
        manager._processes["health-race"] = health_race
        ready_publish_entered = threading.Event()
        release_ready_publish = threading.Event()
        terminal_committed = threading.Event()
        original_record_ready = manager.health_manager.record_ready

        def blocking_record_ready(name: str) -> None:
            ready_publish_entered.set()
            assert release_ready_publish.wait(timeout=2)
            original_record_ready(name)

        monkeypatch.setattr(
            manager.health_manager, "record_ready", blocking_record_ready
        )
        ready_thread = threading.Thread(
            target=manager._commit_ready,
            args=(health_race,),
        )

        def exit_generation() -> None:
            assert manager._commit_terminal(
                health_race,
                importlib.import_module("haniel.core.process").ReadinessState.EXITED,
            )
            manager.health_manager.record_crash("health-race", 1)
            terminal_committed.set()

        exit_thread = threading.Thread(target=exit_generation)
        ready_thread.start()
        assert ready_publish_entered.wait(timeout=2), "ready-health-publish-entered"
        exit_thread.start()
        try:
            assert not terminal_committed.wait(timeout=0.1), (
                "health-publish-inside-generation-lock"
            )
        finally:
            release_ready_publish.set()
            ready_thread.join(timeout=2)
            exit_thread.join(timeout=2)
        assert not ready_thread.is_alive()
        assert not exit_thread.is_alive()
        assert health_race.readiness_state.value == "exited"
        assert (
            manager.health_manager.get_health("health-race").state
            is ServiceState.CRASHED
        )

        stale_crash = ManagedProcess(
            name="stale-crash",
            config=_config("python old.py", f"log:{MARKER}"),
            process=process,
            ready_event=threading.Event(),
            generation=14,
        )
        current_crash = ManagedProcess(
            name="stale-crash",
            config=_config("python new.py", f"log:{MARKER}"),
            process=process,
            ready_event=threading.Event(),
            generation=15,
        )
        manager._processes["stale-crash"] = current_crash
        assert manager._commit_crash(stale_crash, 1) is False
        assert (
            manager.health_manager.get_health("stale-crash").state
            is ServiceState.STOPPED
        )

        for name, terminal in (
            ("monitor-exit-first", "exited"),
            ("timeout-before-crash", "timed_out"),
        ):
            crashed = ManagedProcess(
                name=name,
                config=_config("python app.py", f"log:{MARKER}"),
                process=process,
                ready_event=threading.Event(),
                generation=16,
            )
            manager._processes[name] = crashed
            manager.health_manager.record_start(name)
            terminal_state = getattr(
                importlib.import_module("haniel.core.process").ReadinessState,
                terminal.upper(),
            )
            assert manager._commit_terminal(
                crashed,
                terminal_state,
                record_running=terminal == "timed_out",
            )
            assert manager._commit_crash(crashed, 23), terminal
            assert crashed.readiness_state is terminal_state
            assert manager.health_manager.get_health(name).state is ServiceState.CRASHED
    finally:
        manager.stop_service("app", force=True)


def test_timeout_commit_rechecks_timely_marker_evidence_under_lock(
    tmp_path: Path,
) -> None:
    readiness = importlib.import_module("haniel.config.readiness")
    manager = _manager(tmp_path)
    process = MagicMock(pid=103)
    process.poll.return_value = None
    managed = ManagedProcess(
        name="deadline-race",
        config=_config("python app.py", f"log:{MARKER}"),
        process=process,
        ready_event=threading.Event(),
        generation=21,
        ready_condition=readiness.parse_ready_condition(f"log:{MARKER}"),
        readiness_started_at=1.0,
        readiness_deadline=10.0,
        marker_observed_at=9.0,
    )
    manager._processes[managed.name] = managed
    manager.health_manager.record_start(managed.name)

    manager._commit_terminal(
        managed,
        importlib.import_module("haniel.core.process").ReadinessState.TIMED_OUT,
        record_running=True,
    )

    assert managed.readiness_state.value == "ready"
    assert managed.ready_event.is_set()
    assert managed.readiness_done_event.is_set()
    assert manager.health_manager.get_health(managed.name).state is ServiceState.READY


def test_marker_evidence_callback_never_waits_for_manager_lock(tmp_path: Path) -> None:
    readiness = importlib.import_module("haniel.config.readiness")
    manager = _manager(tmp_path)
    capture = manager.log_manager.start_capture("marker-lock")
    process = MagicMock(pid=104)
    process.poll.return_value = None
    managed = ManagedProcess(
        name="marker-lock",
        config=_config("python app.py", f"log:{MARKER}"),
        process=process,
        log_capture=capture,
        ready_event=threading.Event(),
        generation=22,
        ready_condition=readiness.parse_ready_condition(f"log:{MARKER}"),
    )
    manager._processes[managed.name] = managed
    manager._arm_log_evidence_locked(managed)
    completed = threading.Event()

    def emit() -> None:
        capture.write_line(MARKER)
        completed.set()

    with manager._lock:
        emitter = threading.Thread(target=emit)
        emitter.start()
        assert completed.wait(0.2), "evidence callback must not reacquire manager lock"
    emitter.join(timeout=1)
    capture.stop()


def test_stop_reaps_grandchild_and_closes_reader_threads_and_pipes(
    tmp_path: Path,
) -> None:
    command, status = _child_command(tmp_path, "hold", grandchild=True)
    manager = _manager(tmp_path)
    managed = manager.start_service(
        "process-tree",
        _config(command, "delay:0.01"),
        ready_timeout=1,
    )
    process = managed.process
    assert process is not None
    assert _wait(status.exists)
    grandchild_pid = int(_status(status)["grandchild_pid"])

    assert manager.stop_service("process-tree", force=True)

    assert not _process_is_running(grandchild_pid)
    assert managed.stdout_reader is not None
    assert managed.stderr_reader is not None
    assert not managed.stdout_reader.is_alive()
    assert not managed.stderr_reader.is_alive()
    assert process.stdout is not None and process.stdout.closed
    assert process.stderr is not None and process.stderr.closed


@pytest.mark.skipif(os.name == "nt", reason="POSIX escaped-session pipe contract")
def test_stop_self_wakes_readers_when_escaped_descendant_holds_pipe(
    tmp_path: Path,
) -> None:
    resources_before = _process_resource_count()
    command, status = _child_command(tmp_path, "hold", escaped_grandchild=True)
    manager = _manager(tmp_path)
    managed = manager.start_service(
        "escaped-tree",
        _config(command, "delay:0.01"),
        ready_timeout=1,
    )
    assert _wait(status.exists)
    grandchild_pid = int(_status(status)["grandchild_pid"])
    process = managed.process
    assert process is not None
    stopped = threading.Event()
    result: list[bool] = []

    def stop() -> None:
        result.append(manager.stop_service("escaped-tree", force=True))
        stopped.set()

    stop_thread = threading.Thread(target=stop, daemon=True)
    stop_thread.start()
    try:
        assert stopped.wait(2), "stop must not wait for an escaped pipe writer"
        assert result == [True]
        assert process.poll() is not None
        assert managed.stdout_reader is not None
        assert managed.stderr_reader is not None
        assert not managed.stdout_reader.is_alive()
        assert not managed.stderr_reader.is_alive()
        assert process.stdout is not None and process.stdout.closed
        assert process.stderr is not None and process.stderr.closed
        assert _process_resource_count() <= resources_before + 1
        assert not any(
            thread.name.startswith("haniel-stream-") and thread.is_alive()
            for thread in threading.enumerate()
        )

        journal = DeploymentStateStore(tmp_path / ".haniel" / "deployments")
        journal.begin("escaped-tree", "old", "new", "release-1")
        journal.transition("escaped-tree", "failed")
        assert journal.read("escaped-tree")["state"] == "failed"
    finally:
        if _process_is_running(grandchild_pid):
            os.kill(grandchild_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        stop_thread.join(timeout=5)


def test_stream_reader_self_wakes_while_an_independent_writer_remains_open(
    tmp_path: Path,
) -> None:
    resources_before = _process_resource_count()
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "r", encoding="utf-8")
    capture = _manager(tmp_path).log_manager.start_capture("independent-writer")
    reader = StreamReader(stream, capture)
    reader.start()
    try:
        reader.stop()
        reader.join(timeout=1)
        assert not reader.is_alive()
        assert stream.closed

        journal = DeploymentStateStore(tmp_path / ".haniel" / "direct-reader")
        journal.begin("reader", "old", "new", "release-1")
        journal.transition("reader", "failed")
        assert journal.read("reader")["state"] == "failed"
    finally:
        os.close(write_fd)
        capture.stop()
    assert _process_resource_count() <= resources_before + 1


def test_missing_ready_is_running_but_explicitly_unconfigured(tmp_path: Path) -> None:
    command, status = _child_command(tmp_path, "hold")
    manager = _manager(tmp_path)
    managed = manager.start_service("legacy", _config(command, None), ready_timeout=1)
    try:
        assert _wait(status.exists)
        assert manager.health_manager.get_health("legacy").state is ServiceState.RUNNING
        assert managed.readiness_state.value == "unconfigured"
        assert manager.wait_for_ready("legacy", timeout=0.01) is False
        runner = _runner_with_manager(tmp_path, manager, "legacy", managed.config)
        assert runner._collect_services_info()[0]["ready"] is False
    finally:
        manager.stop_service("legacy", force=True)


def test_ready_monitor_exception_commits_terminal_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    process = MagicMock(pid=102)
    process.poll.return_value = None
    managed = ManagedProcess(
        name="probe-error",
        config=_config("python app.py", "port:8080"),
        process=process,
        ready_event=threading.Event(),
        generation=20,
        ready_condition=importlib.import_module(
            "haniel.config.readiness"
        ).parse_ready_condition("port:8080"),
        readiness_started_at=1.0,
        readiness_deadline=10.0,
    )
    manager._processes[managed.name] = managed
    monkeypatch.setattr(
        manager,
        "_check_ready_condition",
        MagicMock(side_effect=OSError("probe infrastructure failed")),
    )
    monkeypatch.setattr("haniel.core.process.time.monotonic", lambda: 2.0)

    manager._ready_monitor_loop(managed)

    assert managed.readiness_state.value != "pending"
    assert managed.readiness_done_event.is_set()
    assert managed.ready_callback_handle is None


def test_stream_reader_start_failure_reaps_process_and_detaches_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command, _status_path = _child_command(tmp_path, "hold")
    manager = _manager(tmp_path)
    monkeypatch.setattr(
        StreamReader,
        "start",
        MagicMock(side_effect=RuntimeError("reader start failed")),
    )

    with pytest.raises(RuntimeError, match="reader start failed"):
        manager.start_service(
            "reader-error",
            _config(command, f"log:{MARKER}"),
            ready_timeout=1,
        )

    managed = manager._processes["reader-error"]
    try:
        assert managed.readiness_state.value != "pending"
        assert managed.readiness_done_event.is_set()
        assert managed.ready_callback_handle is None
        assert managed.log_capture is not None
        assert managed.log_capture.callback_count == 0
        assert managed.process is None or managed.process.poll() is not None
    finally:
        manager.stop_all()


def test_invalid_reload_preserves_resident_snapshot_and_generation(
    tmp_path: Path,
) -> None:
    """R7: reload validates before replacing any resident identity."""
    validators = importlib.import_module("haniel.config.validators")
    semantic_error = getattr(validators, "ConfigSemanticError", RuntimeError)
    config_path = tmp_path / "haniel.yaml"
    valid_service = ServiceConfig(run="python app.py", ready="delay:0.01")
    valid = HanielConfig(services={"app": valid_service})
    _dump_config(config_path, valid)
    runner = ServiceRunner(valid, tmp_path, config_path=config_path)
    process = MagicMock(pid=1234)
    process.poll.return_value = None
    managed = ManagedProcess(
        name="app",
        config=valid_service,
        process=process,
        ready_event=threading.Event(),
        generation=7,
    )
    runner.process_manager._processes["app"] = managed
    original_config = runner.config
    original_service = runner._enabled_services["app"]

    invalid = valid.model_copy(deep=True)
    invalid.services["app"] = invalid.services["app"].model_copy(
        update={"ready": "port:0"}
    )
    _dump_config(config_path, invalid)

    with pytest.raises(semantic_error):
        runner.reload_config()
    assert runner.config is original_config
    assert runner._enabled_services["app"] is original_service
    assert runner.process_manager._processes["app"] is managed
    assert runner.process_manager.get_pid("app") == 1234
    assert managed.generation == 7


def test_invalid_handover_preserves_resident_snapshot_and_generation(
    tmp_path: Path,
) -> None:
    """R8: exact handover bytes pass semantic validation before projection."""
    handover = importlib.import_module("haniel.core.handover_config")
    config_path = tmp_path / "haniel.yaml"
    invalid = HanielConfig(
        services={"app": ServiceConfig(run="python app.py", ready="port:0")}
    )
    before = _dump_config(config_path, invalid)
    callbacks: list[str] = []

    with pytest.raises(handover.HandoverConfigError):
        handover._load_config_projection(config_path)
    assert config_path.read_bytes() == before
    assert callbacks == []


def test_live_socket_and_late_marker_after_timeout_stay_not_ready(
    tmp_path: Path,
) -> None:
    """I1: a live TCP child is not readiness evidence for a log probe."""
    release = tmp_path / "late.marker"
    command, status = _child_command(tmp_path, "tcp-before-log", release=release)
    manager = _manager(tmp_path)
    config = _config(command, f"log:{MARKER}")
    managed = manager.start_service("app", config, ready_timeout=0.05)
    capture = managed.log_capture
    runner = _runner_with_manager(tmp_path, manager, "app", config)
    try:
        assert _wait(status.exists)
        port = int(_status(status)["port"])
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        assert manager.wait_for_ready("app", timeout=0.2) is False
        assert runner._collect_services_info()[0]["ready"] is False
        release.touch()
        assert _wait(lambda: bool(_status(status).get("marker_emitted")))
        assert manager.health_manager.get_health("app").state is not ServiceState.READY
        assert runner._collect_services_info()[0]["ready"] is False
    finally:
        manager.stop_service("app", force=True)
    assert capture is not None
    assert len(capture._pattern_callbacks) == 0
    monitor = getattr(managed, "ready_monitor", None)
    assert monitor is None or not monitor.is_alive()


def test_http_200_before_readiness_marker_stays_not_ready(tmp_path: Path) -> None:
    """I2: service-owned HTTP is not a substitute for the configured log marker."""
    release = tmp_path / "http.marker"
    command, status = _child_command(tmp_path, "http-before-log", release=release)
    manager = _manager(tmp_path)
    managed = manager.start_service(
        "app", _config(command, f"log:{MARKER}"), ready_timeout=2
    )
    try:
        assert _wait(status.exists)
        port = int(_status(status)["port"])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
            assert r.status == 200
        assert not managed.ready_event.is_set()
        release.touch()
        assert manager.wait_for_ready("app", timeout=2)
        assert manager.health_manager.get_health("app").state is ServiceState.READY
    finally:
        manager.stop_service("app", force=True)


def test_collect_services_info_ready_matches_current_probe_transition(
    tmp_path: Path,
) -> None:
    """I3: external ready follows only the current process probe transition."""
    release = tmp_path / "collector.marker"
    command, status = _child_command(tmp_path, "tcp-before-log", release=release)
    manager = _manager(tmp_path)
    config = _config(command, f"log:{MARKER}")
    runner = _runner_with_manager(tmp_path, manager, "app", config)
    manager.start_service("app", config, ready_timeout=2)
    try:
        assert _wait(status.exists)
        assert runner._collect_services_info()[0]["ready"] is False
        release.touch()
        assert manager.wait_for_ready("app", timeout=2)
        assert runner._collect_services_info()[0]["ready"] is True
        assert manager.stop_service("app", force=True)
        assert runner._collect_services_info()[0]["ready"] is False
    finally:
        manager.stop_service("app", force=True)
