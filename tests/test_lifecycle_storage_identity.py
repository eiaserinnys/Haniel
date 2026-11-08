"""Process start identity resolution across platforms."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from haniel.core.lifecycle_control import LifecycleControl
from haniel.core.lifecycle_locks import LifecycleConflict
from haniel.core.lifecycle_storage import process_start_identity


def _ps_result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["ps"], returncode=returncode, stdout=stdout, stderr=""
    )


def test_darwin_reads_ps_start_time() -> None:
    with (
        patch("haniel.core.lifecycle_storage.os.name", "posix"),
        patch("haniel.core.lifecycle_storage.sys.platform", "darwin"),
        patch(
            "haniel.core.lifecycle_storage.subprocess.run",
            return_value=_ps_result("Sun Nov  8 01:48:43 2026\n"),
        ) as run,
    ):
        identity = process_start_identity(4242)

    assert identity == "ps-start-utc:2026-11-08T01:48:43"
    args, kwargs = run.call_args
    assert args[0] == ["ps", "-p", "4242", "-o", "lstart="]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is False
    assert kwargs["timeout"] == 2
    assert kwargs["env"]["LC_ALL"] == "C"
    assert kwargs["env"]["TZ"] == "UTC0"


def test_darwin_ps_single_digit_day_with_padding() -> None:
    with (
        patch("haniel.core.lifecycle_storage.os.name", "posix"),
        patch("haniel.core.lifecycle_storage.sys.platform", "darwin"),
        patch(
            "haniel.core.lifecycle_storage.subprocess.run",
            return_value=_ps_result("Mon Jan  5 07:08:09 2026\n"),
        ),
    ):
        identity = process_start_identity(7)

    assert identity == "ps-start-utc:2026-01-05T07:08:09"


def test_darwin_ps_parsing_ignores_process_locale(monkeypatch) -> None:
    monkeypatch.setenv("LANG", "ko_KR.UTF-8")
    monkeypatch.setenv("LC_ALL", "ko_KR.UTF-8")
    with (
        patch("haniel.core.lifecycle_storage.os.name", "posix"),
        patch("haniel.core.lifecycle_storage.sys.platform", "darwin"),
        patch(
            "haniel.core.lifecycle_storage.subprocess.run",
            return_value=_ps_result("Tue Dec 30 23:59:58 2025\n"),
        ) as run,
    ):
        identity = process_start_identity(99)

    assert identity == "ps-start-utc:2025-12-30T23:59:58"
    env = run.call_args.kwargs["env"]
    assert env["LC_ALL"] == "C"
    assert env["TZ"] == "UTC0"
    assert env["LANG"] == "ko_KR.UTF-8"


@pytest.mark.parametrize(
    "result",
    [
        _ps_result(""),
        _ps_result("Sun Nov  8 01:48:43 2026\n", returncode=1),
    ],
)
def test_darwin_ps_failure_returns_none(result: subprocess.CompletedProcess) -> None:
    with (
        patch("haniel.core.lifecycle_storage.os.name", "posix"),
        patch("haniel.core.lifecycle_storage.sys.platform", "darwin"),
        patch("haniel.core.lifecycle_storage.subprocess.run", return_value=result),
    ):
        assert process_start_identity(4242) is None


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired(cmd="ps", timeout=2),
        OSError("ps is unavailable"),
        subprocess.SubprocessError("ps failed"),
    ],
)
def test_darwin_ps_exception_returns_none(error: Exception) -> None:
    with (
        patch("haniel.core.lifecycle_storage.os.name", "posix"),
        patch("haniel.core.lifecycle_storage.sys.platform", "darwin"),
        patch("haniel.core.lifecycle_storage.subprocess.run", side_effect=error),
    ):
        assert process_start_identity(4242) is None


@pytest.mark.parametrize(
    "stdout",
    [
        "Sun Nov 8 01:48:43\n",
        "Sun Nov 8 01:48 2026\n",
        "Xyz Nov 8 01:48:43 2026\n",
        "Sun Foo 8 01:48:43 2026\n",
        "Sun Nov 8 01:48:43 2026\nMon Nov 9 01:48:43 2026\n",
        "Sun Feb 30 01:48:43 2026\n",
        "Sun Nov 8 25:00:00 2026\n",
    ],
)
def test_darwin_ps_malformed_output_returns_none(stdout: str) -> None:
    with (
        patch("haniel.core.lifecycle_storage.os.name", "posix"),
        patch("haniel.core.lifecycle_storage.sys.platform", "darwin"),
        patch(
            "haniel.core.lifecycle_storage.subprocess.run",
            return_value=_ps_result(stdout),
        ),
    ):
        assert process_start_identity(4242) is None


def test_linux_proc_stat_takes_priority_over_ps() -> None:
    stat_line = "4242 (worker) S " + " ".join(str(value) for value in range(1, 21))
    with (
        patch("haniel.core.lifecycle_storage.os.name", "posix"),
        patch("haniel.core.lifecycle_storage.sys.platform", "linux"),
        patch("haniel.core.lifecycle_storage.Path.read_text", return_value=stat_line),
        patch("haniel.core.lifecycle_storage.subprocess.run") as run,
    ):
        identity = process_start_identity(4242)

    assert identity == "proc-start-ticks:19"
    run.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError(2, "no proc"), PermissionError(13, "denied")],
)
def test_missing_procfs_falls_back_to_ps(error: Exception) -> None:
    with (
        patch("haniel.core.lifecycle_storage.os.name", "posix"),
        patch("haniel.core.lifecycle_storage.sys.platform", "linux"),
        patch("haniel.core.lifecycle_storage.Path.read_text", side_effect=error),
        patch(
            "haniel.core.lifecycle_storage.subprocess.run",
            return_value=_ps_result("Sun Nov  8 01:48:43 2026\n"),
        ),
    ):
        identity = process_start_identity(5)

    assert identity == "ps-start-utc:2026-11-08T01:48:43"


def test_short_proc_stat_falls_back_to_ps() -> None:
    with (
        patch("haniel.core.lifecycle_storage.os.name", "posix"),
        patch("haniel.core.lifecycle_storage.sys.platform", "linux"),
        patch(
            "haniel.core.lifecycle_storage.Path.read_text",
            return_value="4242 (worker) S 1 2 3",
        ),
        patch(
            "haniel.core.lifecycle_storage.subprocess.run",
            return_value=_ps_result("Sun Nov  8 01:48:43 2026\n"),
        ),
    ):
        identity = process_start_identity(4242)

    assert identity == "ps-start-utc:2026-11-08T01:48:43"


def test_windows_dispatch_uses_filetime_identity() -> None:
    with (
        patch("haniel.core.lifecycle_storage.os.name", "nt"),
        patch(
            "haniel.core.lifecycle_storage._windows_process_start_identity",
            return_value="windows-filetime:7",
        ) as windows_identity,
    ):
        identity = process_start_identity(77)

    assert identity == "windows-filetime:7"
    windows_identity.assert_called_once_with(77)


@pytest.mark.parametrize("pid", [0, -1])
def test_non_positive_pid_returns_none(pid: int) -> None:
    with patch("haniel.core.lifecycle_storage.subprocess.run") as run:
        assert process_start_identity(pid) is None
    run.assert_not_called()


def test_stored_ps_identity_mismatch_conflicts(tmp_path: Path) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")

    with control.acquire_owner("instance-a"):
        metadata = control.read_owner()
        metadata["process_start_identity"] = "ps-start-utc:2026-11-08T01:48:43"
        control.owner_path.write_text(json.dumps(metadata), encoding="utf-8")

        with patch(
            "haniel.core.lifecycle_control.process_start_identity",
            return_value="ps-start-utc:2026-11-08T01:48:44",
        ):
            with pytest.raises(LifecycleConflict) as conflict:
                control.read_active_owner()

    assert conflict.value.code == "LIFECYCLE_OWNER_CONFLICT"
