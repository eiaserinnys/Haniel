"""Lifecycle service commands wait for a deployment that may touch the service."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from haniel.config.model import HanielConfig
from haniel.core.runner import ServiceRunner


def _runner(tmp_path: Path) -> ServiceRunner:
    payload = {
        "poll_interval": 10,
        "repos": {
            "appA": {
                "url": "git@github.com:test/appA.git",
                "path": "appA",
                "branch": "main",
            },
        },
        "services": {
            "web": {"run": "echo web", "repo": "appA"},
            "api": {"run": "echo api", "after": ["web"]},
            "lone": {"run": "echo lone"},
        },
    }
    config = HanielConfig.model_validate(payload)
    return ServiceRunner(config=config, config_dir=tmp_path)


def _run_in_thread(
    runner: ServiceRunner, *args
) -> tuple[threading.Thread, threading.Event]:
    done = threading.Event()

    def target() -> None:
        try:
            runner._handle_service_command(*args)
        finally:
            done.set()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, done


@pytest.mark.parametrize(
    ("action", "attribute"),
    [
        ("restart", "restart_service"),
        ("stop", "stop_service"),
        ("start", "_start_service"),
    ],
)
def test_lifecycle_command_waits_for_the_service_repo_deployment(
    tmp_path: Path, action: str, attribute: str
) -> None:
    runner = _runner(tmp_path)
    operation = MagicMock(return_value=True)
    setattr(runner, attribute, operation)
    lock = runner._pull_locks["appA"]
    lock.acquire()
    try:
        _thread, done = _run_in_thread(runner, "web", action)
        assert not done.wait(timeout=0.3), (
            "command ran while the deployment held the repo"
        )
        operation.assert_not_called()
    finally:
        lock.release()
    assert done.wait(timeout=5)
    operation.assert_called_once_with("web")


def test_dependent_service_waits_for_the_upstream_repo_deployment(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    runner.restart_service = MagicMock(return_value="ok")
    lock = runner._pull_locks["appA"]
    lock.acquire()
    try:
        _thread, done = _run_in_thread(runner, "api", "restart")
        assert not done.wait(timeout=0.3)
        runner.restart_service.assert_not_called()
    finally:
        lock.release()
    assert done.wait(timeout=5)
    runner.restart_service.assert_called_once_with("api")


def test_reload_waits_for_the_service_repo_deployment(
    tmp_path: Path, monkeypatch
) -> None:
    import haniel.core.service_lifecycle as lifecycle

    runner = _runner(tmp_path)
    reload = MagicMock(return_value={"ok": True, "service": "web"})
    monkeypatch.setattr(lifecycle, "reload_service_definition", reload)
    lock = runner._pull_locks["appA"]
    lock.acquire()
    try:
        _thread, done = _run_in_thread(runner, "web", "reload")
        assert not done.wait(timeout=0.3)
        reload.assert_not_called()
    finally:
        lock.release()
    assert done.wait(timeout=5)
    reload.assert_called_once_with(runner, "web")


def test_unrelated_service_and_config_commands_do_not_wait(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    runner.restart_service = MagicMock(return_value="ok")
    runner.reload_config = MagicMock()
    lock = runner._pull_locks["appA"]
    lock.acquire()
    try:
        _thread, done = _run_in_thread(runner, "lone", "restart")
        assert done.wait(timeout=5)
        runner.restart_service.assert_called_once_with("lone")
        _thread, done = _run_in_thread(runner, "config", "reload-config")
        assert done.wait(timeout=5)
        runner.reload_config.assert_called_once()
    finally:
        lock.release()
