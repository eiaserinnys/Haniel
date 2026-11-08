"""POSIX process tree, listener ownership and termination contracts."""

from __future__ import annotations

import signal
import subprocess
from unittest.mock import MagicMock, mock_open, patch

import pytest

from haniel.platform import get_platform_handler
from haniel.platform.posix import PosixHandler


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["ps"], returncode=returncode, stdout=stdout, stderr=""
    )


def test_get_process_tree_uses_single_ps_snapshot() -> None:
    snapshot = "\n".join(
        [
            "    1     0",
            "  100     1",
            "  101   100",
            "  200     1",
            "  102   101",
        ]
    )
    with patch(
        "haniel.platform.posix.subprocess.run",
        return_value=_completed(snapshot + "\n"),
    ) as run:
        tree = PosixHandler._get_process_tree(100)

    assert tree == {100, 101, 102}
    args, kwargs = run.call_args
    assert args[0] == ["ps", "-axo", "pid=,ppid="]
    assert kwargs["timeout"] == 2
    assert kwargs["check"] is False
    assert run.call_count == 1


def test_get_process_tree_skips_malformed_rows() -> None:
    snapshot = "\n".join(
        [
            "  100     1",
            "  101   100",
            "not a pid line",
            "42",
            "0 1",
            "50 0",
            "abc 100",
            "103   100",
        ]
    )
    with patch(
        "haniel.platform.posix.subprocess.run",
        return_value=_completed(snapshot + "\n"),
    ):
        tree = PosixHandler._get_process_tree(100)

    assert tree == {100, 101, 103}


@pytest.mark.parametrize(
    "result",
    [
        _completed("  101   100\n", returncode=1),
        _completed(""),
    ],
)
def test_get_process_tree_returns_root_without_snapshot(
    result: subprocess.CompletedProcess,
) -> None:
    with patch("haniel.platform.posix.subprocess.run", return_value=result):
        assert PosixHandler._get_process_tree(100) == {100}


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired(cmd="ps", timeout=2),
        OSError("ps is unavailable"),
    ],
)
def test_get_process_tree_returns_root_on_error(error: Exception) -> None:
    with patch("haniel.platform.posix.subprocess.run", side_effect=error):
        assert PosixHandler._get_process_tree(100) == {100}


@pytest.mark.parametrize(
    ("listener_pids", "expected"),
    [
        ({100}, True),
        ({101}, True),
        ({102}, True),
        ({200}, False),
        (set(), False),
    ],
)
def test_port_ownership_uses_process_tree(
    listener_pids: set[int], expected: bool
) -> None:
    snapshot = "\n".join(["  100     1", "  101   100", "  200     1", "  102   101"])
    handler = PosixHandler()
    with (
        patch.object(handler, "_get_listening_pids", return_value=listener_pids),
        patch(
            "haniel.platform.posix.subprocess.run",
            return_value=_completed(snapshot + "\n"),
        ),
    ):
        assert handler.is_port_owned_by_process_tree(8080, 100) is expected


def test_lsof_listener_lookup_intersects_address_and_state() -> None:
    with patch(
        "haniel.platform.posix.subprocess.run",
        return_value=_completed("101\n101\n\nnot-a-pid\n202\n"),
    ) as run:
        pids = PosixHandler._get_listening_pids_from_lsof(8080)

    assert pids == {101, 202}
    args, kwargs = run.call_args
    assert args[0] == [
        "lsof",
        "-nP",
        "-a",
        "-iTCP:8080",
        "-sTCP:LISTEN",
        "-t",
    ]
    assert kwargs["timeout"] == 2
    assert kwargs["check"] is False


@pytest.mark.parametrize(
    "result",
    [
        _completed("101\n", returncode=1),
        _completed(""),
    ],
)
def test_lsof_listener_lookup_returns_empty_without_matches(
    result: subprocess.CompletedProcess,
) -> None:
    with patch("haniel.platform.posix.subprocess.run", return_value=result):
        assert PosixHandler._get_listening_pids_from_lsof(8080) == set()


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired(cmd="lsof", timeout=2),
        OSError("lsof is unavailable"),
    ],
)
def test_lsof_listener_lookup_returns_empty_on_error(error: Exception) -> None:
    with patch("haniel.platform.posix.subprocess.run", side_effect=error):
        assert PosixHandler._get_listening_pids_from_lsof(8080) == set()


def test_process_command_line_prefers_procfs() -> None:
    handler = PosixHandler()
    with (
        patch("builtins.open", mock_open(read_data=b"python\x00app.py")),
        patch("haniel.platform.posix.subprocess.run") as run,
    ):
        assert handler.get_process_command_line(42) == "python app.py"
    run.assert_not_called()


def test_process_command_line_ps_fallback_returns_none_on_nonzero() -> None:
    handler = PosixHandler()
    with (
        patch("builtins.open", side_effect=FileNotFoundError(2, "no procfs")),
        patch(
            "haniel.platform.posix.subprocess.run",
            return_value=_completed("python app.py\n", returncode=1),
        ),
    ):
        assert handler.get_process_command_line(42) is None


def test_process_command_line_ps_fallback_returns_command() -> None:
    handler = PosixHandler()
    with (
        patch("builtins.open", side_effect=FileNotFoundError(2, "no procfs")),
        patch(
            "haniel.platform.posix.subprocess.run",
            return_value=_completed("python app.py\n"),
        ),
    ):
        assert handler.get_process_command_line(42) == "python app.py"


def test_terminate_process_signals_the_process_group() -> None:
    handler = PosixHandler()
    process = MagicMock()
    process.poll.return_value = None
    process.pid = 4321
    with (
        patch("haniel.platform.posix.os.getpgid", return_value=4321),
        patch("haniel.platform.posix.os.killpg") as killpg,
    ):
        handler.terminate_process(process)

    killpg.assert_called_once_with(4321, signal.SIGTERM)
    process.terminate.assert_not_called()


def test_kill_process_signals_the_process_group() -> None:
    handler = PosixHandler()
    process = MagicMock()
    process.poll.return_value = None
    process.pid = 4321
    with (
        patch("haniel.platform.posix.os.getpgid", return_value=4321),
        patch("haniel.platform.posix.os.killpg") as killpg,
    ):
        handler.kill_process(process)

    killpg.assert_called_once_with(4321, signal.SIGKILL)
    process.kill.assert_not_called()


def test_terminate_process_falls_back_when_group_is_gone() -> None:
    handler = PosixHandler()
    process = MagicMock()
    process.poll.return_value = None
    process.pid = 4321
    with patch("haniel.platform.posix.os.getpgid", side_effect=ProcessLookupError):
        handler.terminate_process(process)

    process.terminate.assert_called_once_with()


def test_kill_process_falls_back_when_group_is_gone() -> None:
    handler = PosixHandler()
    process = MagicMock()
    process.poll.return_value = None
    process.pid = 4321
    with patch("haniel.platform.posix.os.getpgid", side_effect=ProcessLookupError):
        handler.kill_process(process)

    process.kill.assert_called_once_with()


def test_darwin_uses_the_posix_handler() -> None:
    with patch("haniel.platform.sys.platform", "darwin"):
        assert isinstance(get_platform_handler(), PosixHandler)
