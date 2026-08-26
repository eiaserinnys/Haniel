"""Contracts for preparing a self-update before managed services stop."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from haniel.config import RepoConfig
from haniel.core.self_update_prestage import (
    SelfUpdatePrestageError,
    SelfUpdatePrestager,
)


TARGET = "a" * 40


def _repo() -> RepoConfig:
    return RepoConfig(
        url="git@github.com:test/haniel.git",
        branch="main",
        path="repo",
    )


class _FinishedProcess:
    def __init__(self) -> None:
        self.stderr = io.StringIO(
            "[haniel-release] step=release_ready status=ok duration_sec=1.250\n"
        )
        self.returncode = 0
        self.pid = 101

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


def _ready_result(config_dir: Path, release_root: Path) -> Path:
    release = release_root / "releases" / TARGET[:12]
    release.mkdir(parents=True)
    (release / ".haniel-release-ready.json").write_text(
        json.dumps({"version": 1, "commit": TARGET}), encoding="utf-8"
    )
    result_path = config_dir / ".local" / "haniel_release_preparation.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(
            {
                "version": 1,
                "ok": True,
                "pre_staged": True,
                "target_commit": TARGET,
                "prepared_release": str(release),
                "active_repo": str(release_root / "releases" / ("b" * 12)),
                "active_python": "/python",
                "active_commit": "b" * 40,
                "switched": False,
                "steps": [],
                "warnings": [],
                "error": None,
            }
        ),
        encoding="utf-8",
    )
    return release


def test_prepare_uses_exact_target_sanitized_env_and_logs_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="haniel.core.self_update_prestage")
    config_dir = tmp_path
    source = config_dir / "repo"
    source.mkdir()
    release_root = config_dir / "releases-root"
    (config_dir / "haniel-runner.conf").write_text(
        "HANIEL_RELEASE_ROOT=releases-root\n", encoding="utf-8"
    )
    captured: dict[str, object] = {}

    def popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        _ready_result(config_dir, release_root)
        return _FinishedProcess()

    monkeypatch.setenv("PATH", "/nodejs:/pnpm")
    monkeypatch.setenv("PYTHONUTF8", "1")
    monkeypatch.setenv("NODE_CHANNEL_FD", "3")
    monkeypatch.setenv("NODE_CHANNEL_SERIALIZATION_MODE", "json")
    monkeypatch.setenv("NODE_UNIQUE_ID", "pm2")
    prestager = SelfUpdatePrestager(config_dir, popen_factory=popen)

    result = prestager.prepare(_repo(), target_commit=TARGET, timeout=3600)

    command = captured["command"]
    assert command[1:4] == [str(prestager.helper_path), "prepare", "--no-switch"]
    assert command[command.index("--target-commit") + 1] == TARGET
    assert result.target_commit == TARGET
    assert result.prepared_release == release_root / "releases" / TARGET[:12]
    env = captured["env"]
    assert env["PATH"] == "/nodejs:/pnpm"
    assert env["PYTHONUTF8"] == "1"
    assert "NODE_CHANNEL_FD" not in env
    assert "NODE_CHANNEL_SERIALIZATION_MODE" not in env
    assert "NODE_UNIQUE_ID" not in env
    assert "release_ready" in caplog.text


def test_freeze_target_fetches_without_advancing_source_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        if command[-2:] == ["fetch", "origin"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, TARGET + "\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    prestager = SelfUpdatePrestager(tmp_path)

    frozen = prestager.freeze_target(_repo(), timeout=60)

    assert frozen == TARGET
    assert calls == [
        ["git", "-C", str(source), "fetch", "origin"],
        ["git", "-C", str(source), "rev-parse", "origin/main^{commit}"],
    ]
    assert all("checkout" not in command and "pull" not in command for command in calls)


def test_duplicate_prepare_cancels_the_single_active_process(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    (tmp_path / "haniel-runner.conf").write_text(
        "HANIEL_RELEASE_ROOT=releases-root\n", encoding="utf-8"
    )
    started = threading.Event()
    released = threading.Event()
    terminated: list[int] = []

    class BlockingProcess(_FinishedProcess):
        def wait(self, timeout: float | None = None) -> int:
            started.set()
            released.wait(2)
            self.returncode = -15
            return self.returncode

    process = BlockingProcess()

    def terminate(candidate, **_kwargs):
        terminated.append(candidate.pid)
        released.set()

    prestager = SelfUpdatePrestager(
        tmp_path,
        popen_factory=lambda *_args, **_kwargs: process,
        terminate_process_tree=terminate,
    )
    first_error: list[BaseException] = []

    def first_attempt() -> None:
        try:
            prestager.prepare(_repo(), target_commit=TARGET, timeout=3600)
        except BaseException as exc:  # noqa: BLE001 - thread assertion capture
            first_error.append(exc)

    worker = threading.Thread(target=first_attempt)
    worker.start()
    assert started.wait(1)

    with pytest.raises(SelfUpdatePrestageError, match="already active"):
        prestager.prepare(_repo(), target_commit=TARGET, timeout=3600)

    worker.join(2)
    assert terminated == [101]
    assert first_error and isinstance(first_error[0], SelfUpdatePrestageError)


def test_cancel_is_idempotent_without_active_process(tmp_path: Path) -> None:
    prestager = SelfUpdatePrestager(tmp_path)

    prestager.cancel()
    prestager.cancel()


def test_cancel_during_spawn_waits_for_publication_reap_and_cleanup(
    tmp_path: Path,
) -> None:
    (tmp_path / "repo").mkdir()
    (tmp_path / "haniel-runner.conf").write_text(
        "HANIEL_RELEASE_ROOT=releases-root\n", encoding="utf-8"
    )
    entered_spawn = threading.Event()
    publish_process = threading.Event()
    terminated = threading.Event()

    class PublishedProcess(_FinishedProcess):
        def wait(self, timeout: float | None = None) -> int:
            assert terminated.wait(2)
            self.returncode = -15
            return self.returncode

    process = PublishedProcess()

    def popen(*_args, **_kwargs):
        entered_spawn.set()
        assert publish_process.wait(2)
        return process

    prestager = SelfUpdatePrestager(
        tmp_path,
        popen_factory=popen,
        terminate_process_tree=lambda active: (
            active.pid == process.pid and terminated.set()
        ),
    )
    prepare_errors: list[BaseException] = []

    def prepare() -> None:
        try:
            prestager.prepare(_repo(), target_commit=TARGET, timeout=30)
        except BaseException as exc:  # noqa: BLE001 - thread assertion capture
            prepare_errors.append(exc)

    prepare_thread = threading.Thread(target=prepare)
    prepare_thread.start()
    assert entered_spawn.wait(1)

    cancel_thread = threading.Thread(target=prestager.cancel)
    cancel_thread.start()
    cancel_thread.join(0.05)
    assert cancel_thread.is_alive(), "cancel must wait for spawn publication"

    publish_process.set()
    cancel_thread.join(2)
    prepare_thread.join(2)

    assert not cancel_thread.is_alive()
    assert not prepare_thread.is_alive()
    assert terminated.is_set()
    assert prepare_errors and isinstance(prepare_errors[0], SelfUpdatePrestageError)
    assert not prestager.result_path.exists()


def test_cancel_cleanup_failure_unblocks_waiter_and_resets_attempt(
    tmp_path: Path,
) -> None:
    (tmp_path / "repo").mkdir()
    (tmp_path / "haniel-runner.conf").write_text(
        "HANIEL_RELEASE_ROOT=releases-root\n", encoding="utf-8"
    )
    started = threading.Event()
    released = threading.Event()

    class BlockingProcess(_FinishedProcess):
        def wait(self, timeout: float | None = None) -> int:
            started.set()
            assert released.wait(2)
            self.returncode = -15
            return self.returncode

    prestager = SelfUpdatePrestager(
        tmp_path,
        popen_factory=lambda *_args, **_kwargs: BlockingProcess(),
        terminate_process_tree=lambda _process: released.set(),
    )
    prestager._cleanup_failed_attempt = MagicMock(  # type: ignore[method-assign]
        side_effect=OSError("candidate is locked")
    )
    prepare_errors: list[BaseException] = []
    cancel_errors: list[BaseException] = []

    def prepare() -> None:
        try:
            prestager.prepare(_repo(), target_commit=TARGET, timeout=30)
        except BaseException as exc:  # noqa: BLE001 - thread assertion capture
            prepare_errors.append(exc)

    prepare_thread = threading.Thread(target=prepare)
    prepare_thread.start()
    assert started.wait(1)

    def cancel() -> None:
        try:
            prestager.cancel()
        except BaseException as exc:  # noqa: BLE001 - thread assertion capture
            cancel_errors.append(exc)

    cancel_thread = threading.Thread(target=cancel, daemon=True)
    cancel_thread.start()
    cancel_thread.join(2)
    prepare_thread.join(2)
    stuck = cancel_thread.is_alive()
    if stuck:
        # Keep the RED test process bounded on the broken implementation.
        with prestager._attempt_condition:
            prestager._attempt_active = False
            prestager._attempt_condition.notify_all()
        cancel_thread.join(1)

    assert not stuck
    assert not prepare_thread.is_alive()
    assert prepare_errors and isinstance(prepare_errors[0], OSError)
    assert not cancel_errors
    prestager.cancel()


def test_cancel_termination_failure_kills_process_waits_for_reset_and_reports_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "repo").mkdir()
    (tmp_path / "haniel-runner.conf").write_text(
        "HANIEL_RELEASE_ROOT=releases-root\n", encoding="utf-8"
    )
    started = threading.Event()
    released = threading.Event()

    class BlockingProcess(_FinishedProcess):
        def wait(self, timeout: float | None = None) -> int:
            started.set()
            assert released.wait(2)
            self.returncode = -9
            return self.returncode

        def kill(self) -> None:
            released.set()

    process = BlockingProcess()
    prestager = SelfUpdatePrestager(
        tmp_path,
        popen_factory=lambda *_args, **_kwargs: process,
        terminate_process_tree=MagicMock(side_effect=OSError("taskkill failed")),
    )
    prepare_errors: list[BaseException] = []

    def prepare() -> None:
        try:
            prestager.prepare(_repo(), target_commit=TARGET, timeout=30)
        except BaseException as exc:  # noqa: BLE001 - thread assertion capture
            prepare_errors.append(exc)

    prepare_thread = threading.Thread(target=prepare)
    prepare_thread.start()
    assert started.wait(1)

    with pytest.raises(SelfUpdatePrestageError, match="could not terminate helper"):
        prestager.cancel()
    prepare_thread.join(2)

    assert not prepare_thread.is_alive()
    assert prepare_errors and isinstance(prepare_errors[0], SelfUpdatePrestageError)
    prestager.cancel()


def test_prepare_snapshots_release_root_for_command_validation_and_cleanup(
    tmp_path: Path,
) -> None:
    (tmp_path / "repo").mkdir()
    (tmp_path / "haniel-runner.conf").write_text(
        "HANIEL_RELEASE_ROOT=release-a\n", encoding="utf-8"
    )
    release_a = tmp_path / "release-a"

    def popen(*_args, **_kwargs):
        _ready_result(tmp_path, release_a)
        (tmp_path / "haniel-runner.conf").write_text(
            "HANIEL_RELEASE_ROOT=release-b\n", encoding="utf-8"
        )
        return _FinishedProcess()

    prestager = SelfUpdatePrestager(tmp_path, popen_factory=popen)

    result = prestager.prepare(_repo(), target_commit=TARGET, timeout=30)

    assert result.prepared_release.parent == release_a / "releases"


def test_timeout_terminates_process_and_removes_unready_candidate(
    tmp_path: Path,
) -> None:
    (tmp_path / "repo").mkdir()
    release_root = tmp_path / "releases-root"
    candidate = release_root / "releases" / TARGET[:12]
    candidate.mkdir(parents=True)
    (candidate / "partial").write_text("unfinished", encoding="utf-8")
    (tmp_path / "haniel-runner.conf").write_text(
        "HANIEL_RELEASE_ROOT=releases-root\n", encoding="utf-8"
    )
    terminated: list[int] = []

    class TimedOutProcess(_FinishedProcess):
        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired(["helper"], timeout)

    process = TimedOutProcess()
    prestager = SelfUpdatePrestager(
        tmp_path,
        popen_factory=lambda *_args, **_kwargs: process,
        terminate_process_tree=lambda active: terminated.append(active.pid),
    )

    with pytest.raises(SelfUpdatePrestageError, match="timed out after 1s"):
        prestager.prepare(_repo(), target_commit=TARGET, timeout=1)

    assert terminated == [101]
    assert not candidate.exists()
    assert not prestager.result_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group contract")
def test_cancel_reaps_real_helper_process_group(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    (tmp_path / "haniel-runner.conf").write_text(
        "HANIEL_RELEASE_ROOT=releases-root\n", encoding="utf-8"
    )
    child_pid_path = tmp_path / "child.pid"
    spawner = tmp_path / "spawn_child.py"
    spawner.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    def popen(_command, **kwargs):
        return subprocess.Popen([sys.executable, str(spawner)], **kwargs)

    prestager = SelfUpdatePrestager(tmp_path, popen_factory=popen)
    errors: list[BaseException] = []

    def prepare() -> None:
        try:
            prestager.prepare(_repo(), target_commit=TARGET, timeout=30)
        except BaseException as exc:  # noqa: BLE001 - thread assertion capture
            errors.append(exc)

    worker = threading.Thread(target=prepare)
    worker.start()
    deadline = time.monotonic() + 2
    while not child_pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_pid_path.exists()
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))

    prestager.cancel()
    worker.join(2)

    assert not worker.is_alive()
    assert errors and isinstance(errors[0], SelfUpdatePrestageError)
    deadline = time.monotonic() + 2
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{child_pid}").exists()


def test_invalid_target_is_rejected_before_spawn(tmp_path: Path) -> None:
    prestager = SelfUpdatePrestager(
        tmp_path,
        popen_factory=lambda *_args, **_kwargs: pytest.fail("must not spawn"),
    )

    with pytest.raises(SelfUpdatePrestageError, match="40-character"):
        prestager.prepare(_repo(), target_commit="abc123", timeout=3600)
