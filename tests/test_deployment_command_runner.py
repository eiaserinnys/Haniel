"""Release manifest command executable resolution tests."""

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from haniel.core.deployment import (
    CommandSpec,
    DeploymentCommandError,
    stable_deployment_error_code,
    subprocess_command_runner,
)


def _python_command(script: Path) -> str:
    argv = [sys.executable, str(script)]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def test_public_classifier_preserves_typed_cause_code() -> None:
    command_error = DeploymentCommandError(
        "COMMAND_EXIT_NONZERO",
        "probe",
        "child failed",
        returncode=7,
    )
    try:
        raise RuntimeError("PREFLIGHT_FAILED: wrapper") from command_error
    except RuntimeError as wrapped:
        assert stable_deployment_error_code(wrapped) == "COMMAND_EXIT_NONZERO"


@pytest.mark.skipif(os.name != "nt", reason="Windows PATHEXT contract")
def test_windows_manifest_command_resolves_and_spawns_cmd_from_path(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "manifest-tool.cmd"
    executable.write_text("@exit /b 0\n", encoding="utf8")

    subprocess_command_runner(tmp_path)(
        CommandSpec(name="verify-board-yjs", command="manifest-tool"),
        {"PATH": str(tmp_path)},
    )


def test_manifest_command_resolves_executable_from_deploy_path(tmp_path: Path) -> None:
    runner = subprocess_command_runner(tmp_path)
    resolved = str(tmp_path / "pnpm.CMD")

    with (
        patch(
            "haniel.core.deployment_command_runner.shutil.which",
            return_value=resolved,
        ) as which,
        patch(
            "haniel.core.deployment_command_runner._run_process_tree",
            return_value=subprocess.CompletedProcess(
                [resolved, "--version"], 0, stdout="", stderr=""
            ),
        ) as run,
    ):
        runner(
            CommandSpec(name="verify-board-yjs", command="pnpm --version"),
            {"PATH": str(tmp_path), "PATHEXT": ".COM;.EXE;.BAT;.CMD"},
        )

    which.assert_called_once_with("pnpm", path=str(tmp_path))
    assert run.call_args.args[0] == [resolved, "--version"]


def test_real_command_preserves_quoted_script_path(tmp_path: Path) -> None:
    spaced = tmp_path / "directory with spaces"
    spaced.mkdir()
    output = spaced / "command output.txt"
    script = spaced / "write output.py"
    script.write_text(
        "import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('ok')\n",
        encoding="utf-8",
    )
    argv = [sys.executable, str(script), str(output)]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)

    subprocess_command_runner(tmp_path)(
        CommandSpec(name="quoted-path", command=command),
        {},
    )

    assert output.read_text(encoding="utf-8") == "ok"


def test_manifest_command_captures_bounded_json_object(tmp_path: Path) -> None:
    runner = subprocess_command_runner(tmp_path)
    resolved = str(tmp_path / "probe")

    with (
        patch(
            "haniel.core.deployment_command_runner.shutil.which",
            return_value=resolved,
        ),
        patch(
            "haniel.core.deployment_command_runner._run_process_tree",
            return_value=subprocess.CompletedProcess(
                [resolved], 0, stdout='{"ok": true}', stderr=""
            ),
        ),
    ):
        result = runner(CommandSpec(name="probe", command="probe"), {})

    assert result is not None
    assert result.json_data == {"ok": True}


def test_manifest_command_rejects_unbounded_json_result(tmp_path: Path) -> None:
    runner = subprocess_command_runner(tmp_path)
    resolved = str(tmp_path / "probe")

    with (
        patch(
            "haniel.core.deployment_command_runner.shutil.which",
            return_value=resolved,
        ),
        patch(
            "haniel.core.deployment_command_runner._run_process_tree",
            return_value=subprocess.CompletedProcess(
                [resolved], 0, stdout="x" * 65537, stderr=""
            ),
        ),
        pytest.raises(RuntimeError, match="JSON result exceeds"),
    ):
        runner(CommandSpec(name="probe", command="probe"), {})


def test_manifest_command_reports_unresolved_executable_name(tmp_path: Path) -> None:
    runner = subprocess_command_runner(tmp_path)

    with (
        patch(
            "haniel.core.deployment_command_runner.shutil.which",
            return_value=None,
        ),
        patch("haniel.core.deployment_command_runner._run_process_tree") as run,
        pytest.raises(RuntimeError) as raised,
    ):
        runner(
            CommandSpec(name="verify-board-yjs", command="missing-tool --verify"),
            {"PATH": str(tmp_path)},
        )

    assert "command 'verify-board-yjs' executable not found: 'missing-tool'" in str(
        raised.value
    )
    run.assert_not_called()


def test_manifest_command_wraps_file_not_found_from_spawn(tmp_path: Path) -> None:
    runner = subprocess_command_runner(tmp_path)
    resolved = str(tmp_path / "pnpm.CMD")

    with (
        patch(
            "haniel.core.deployment_command_runner.shutil.which",
            return_value=resolved,
        ),
        patch(
            "haniel.core.deployment_command_runner._run_process_tree",
            side_effect=FileNotFoundError(2, "file not found", resolved),
        ),
        pytest.raises(RuntimeError) as raised,
    ):
        runner(
            CommandSpec(name="verify-board-yjs", command="pnpm --version"),
            {"PATH": str(tmp_path)},
        )

    message = str(raised.value)
    assert "command 'verify-board-yjs' could not start executable" in message
    assert resolved in message


def test_manifest_command_redacts_sensitive_output_and_environment_values(
    tmp_path: Path,
) -> None:
    runner = subprocess_command_runner(tmp_path)
    resolved = str(tmp_path / "probe")
    token = "token-value-should-not-escape"
    database_url = "postgresql://db-user:db-password@db.example/app"
    stdout = f"TOKEN={token}\nDATABASE_URL={database_url}\nPASSWORD=plain-password"
    stderr = f"AUTH: {token}\nconnect {database_url}\nCREDENTIAL=plain-credential"

    with (
        patch(
            "haniel.core.deployment_command_runner.shutil.which",
            return_value=resolved,
        ),
        patch(
            "haniel.core.deployment_command_runner._run_process_tree",
            side_effect=subprocess.CalledProcessError(
                1,
                [resolved],
                output=stdout,
                stderr=stderr,
            ),
        ),
        pytest.raises(RuntimeError) as raised,
    ):
        runner(
            CommandSpec(name="probe", command="probe"),
            {"AUTH_TOKEN": token, "DATABASE_URL": database_url},
        )

    message = str(raised.value)
    for secret in (
        token,
        database_url,
        "db-user",
        "db-password",
        "plain-password",
        "plain-credential",
    ):
        assert secret not in message
    assert "[REDACTED]" in message


def test_manifest_json_result_is_redacted_before_journal_consumption(
    tmp_path: Path,
) -> None:
    runner = subprocess_command_runner(tmp_path)
    resolved = str(tmp_path / "probe")
    token = "json-secret-value"
    payload = (
        '{"ok": true, "secret": "json-secret-value", '
        '"message": "AUTH_TOKEN=json-secret-value"}'
    )

    with (
        patch(
            "haniel.core.deployment_command_runner.shutil.which",
            return_value=resolved,
        ),
        patch(
            "haniel.core.deployment_command_runner._run_process_tree",
            return_value=subprocess.CompletedProcess(
                [resolved], 0, stdout=payload, stderr=f"TOKEN={token}"
            ),
        ),
    ):
        result = runner(
            CommandSpec(name="probe", command="probe"),
            {"AUTH_TOKEN": token},
        )

    assert result is not None
    serialized = repr(result)
    assert token not in serialized
    assert result.json_data == {
        "ok": True,
        "secret": "[REDACTED]",
        "message": "AUTH_TOKEN=[REDACTED]",
    }


def test_real_nonzero_child_has_stable_code_and_bounded_redacted_evidence(
    tmp_path: Path,
) -> None:
    script = tmp_path / "fail.py"
    script.write_text(
        "import os, sys\n"
        "print('x' * 20000, file=sys.stderr)\n"
        "print('AUTH_TOKEN=' + os.environ['AUTH_TOKEN'], file=sys.stderr)\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    secret = "incident-secret-must-not-escape"

    with pytest.raises(RuntimeError) as raised:
        subprocess_command_runner(tmp_path)(
            CommandSpec(name="probe", command=_python_command(script)),
            {"AUTH_TOKEN": secret},
        )

    error = raised.value
    assert getattr(error, "code", None) == "COMMAND_EXIT_NONZERO"
    assert getattr(error, "command_name", None) == "probe"
    assert getattr(error, "returncode", None) == 7
    assert secret not in str(error)
    assert "[REDACTED]" in str(error)
    assert len(str(error)) < 9000


def test_real_timeout_child_has_stable_code_and_is_reaped(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "sleep.py"
    script.write_text(
        "import os, pathlib, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    with pytest.raises(Exception) as raised:
        subprocess_command_runner(tmp_path)(
            CommandSpec(
                name="probe",
                command=_python_command(script),
                timeout_seconds=1,
            ),
            {},
        )

    error = raised.value
    assert getattr(error, "code", None) == "COMMAND_TIMEOUT"
    assert getattr(error, "command_name", None) == "probe"
    assert pid_file.exists()
    pid = int(pid_file.read_text(encoding="utf-8"))
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.02)
    else:
        pytest.fail(f"timed out command process {pid} is still alive")


def test_real_timeout_reaps_descendant_process_tree(tmp_path: Path) -> None:
    escaped = tmp_path / "descendant-escaped.txt"
    descendant = tmp_path / "descendant.py"
    descendant.write_text(
        "import pathlib, time\n"
        "time.sleep(2)\n"
        f"pathlib.Path({str(escaped)!r}).write_text('escaped')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(descendant)!r}])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    with pytest.raises(DeploymentCommandError) as raised:
        subprocess_command_runner(tmp_path)(
            CommandSpec(
                name="probe",
                command=_python_command(parent),
                timeout_seconds=1,
            ),
            {},
        )

    assert raised.value.code == "COMMAND_TIMEOUT"
    time.sleep(2.5)
    assert not escaped.exists()


def test_real_valid_non_object_json_has_stable_result_code(tmp_path: Path) -> None:
    script = tmp_path / "list.py"
    script.write_text("print('[1, 2, 3]')\n", encoding="utf-8")

    with pytest.raises(RuntimeError) as raised:
        subprocess_command_runner(tmp_path)(
            CommandSpec(name="probe", command=_python_command(script)),
            {},
        )

    assert getattr(raised.value, "code", None) == "COMMAND_RESULT_INVALID"
