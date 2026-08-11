"""Regression tests for bounded shutdown thread joins."""

import logging
import threading
from unittest.mock import MagicMock

from haniel.config import HanielConfig
from haniel.core.lifecycle_request_server import LifecycleRequestServer
from haniel.core.runner import ServiceRunner
from haniel.core.thread_shutdown import (
    DEFAULT_THREAD_JOIN_TIMEOUT_SECONDS,
    join_thread_with_timeout,
)
from haniel.integrations.mcp_server import HanielMcpServer
from haniel.integrations.orchestrator_client import OrchestratorClient
from haniel.integrations.slack_bot import SlackBot


def _stuck_thread() -> MagicMock:
    thread = MagicMock()
    thread.is_alive.return_value = True
    return thread


def test_join_thread_with_timeout_warns_and_returns_false(caplog) -> None:
    thread = _stuck_thread()

    with caplog.at_level(logging.WARNING):
        stopped = join_thread_with_timeout(thread, name="test-worker")

    assert stopped is False
    thread.join.assert_called_once_with(timeout=DEFAULT_THREAD_JOIN_TIMEOUT_SECONDS)
    assert "test-worker did not stop within 5.0s" in caplog.text


def test_slack_stop_bounds_socket_thread_join() -> None:
    bot = object.__new__(SlackBot)
    bot._handler = MagicMock()
    bot._socket_thread = _stuck_thread()

    bot.stop()

    bot._handler.close.assert_called_once_with()
    bot._socket_thread.join.assert_called_once_with(
        timeout=DEFAULT_THREAD_JOIN_TIMEOUT_SECONDS
    )


def test_orchestrator_stop_bounds_connection_thread_join() -> None:
    client = object.__new__(OrchestratorClient)
    client._stop_event = threading.Event()
    client._thread = _stuck_thread()

    client.stop()

    assert client._stop_event.is_set()
    client._thread.join.assert_called_once_with(
        timeout=DEFAULT_THREAD_JOIN_TIMEOUT_SECONDS
    )


def test_mcp_stop_bounds_server_thread_join() -> None:
    server = object.__new__(HanielMcpServer)
    server._stop_requested = threading.Event()
    server._server = MagicMock()
    server._server_thread = _stuck_thread()

    server.stop_sync()

    assert server._stop_requested.is_set()
    assert server._server.should_exit is True
    server._server_thread.join.assert_called_once_with(
        timeout=DEFAULT_THREAD_JOIN_TIMEOUT_SECONDS
    )


def test_lifecycle_request_server_close_bounds_worker_join() -> None:
    server = object.__new__(LifecycleRequestServer)
    server._stopping = threading.Event()
    server._thread = _stuck_thread()

    server.close()

    assert server._stopping.is_set()
    server._thread.join.assert_called_once_with(
        timeout=DEFAULT_THREAD_JOIN_TIMEOUT_SECONDS
    )


def test_runner_stop_bounds_poll_thread_join(tmp_path) -> None:
    runner = ServiceRunner(
        HanielConfig(poll_interval=5, repos={}, services={}),
        config_dir=tmp_path,
    )
    runner._state.running = True
    runner._poll_thread = _stuck_thread()
    runner.stop_services = MagicMock(return_value=True)

    runner.stop()

    runner._poll_thread.join.assert_called_once_with(
        timeout=DEFAULT_THREAD_JOIN_TIMEOUT_SECONDS
    )

