"""Release manifest command executable resolution tests."""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from haniel.core.deployment import CommandSpec, subprocess_command_runner


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
            "haniel.core.deployment_command_runner.subprocess.run",
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


def test_manifest_command_captures_bounded_json_object(tmp_path: Path) -> None:
    runner = subprocess_command_runner(tmp_path)
    resolved = str(tmp_path / "probe")

    with (
        patch(
            "haniel.core.deployment_command_runner.shutil.which",
            return_value=resolved,
        ),
        patch(
            "haniel.core.deployment_command_runner.subprocess.run",
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
            "haniel.core.deployment_command_runner.subprocess.run",
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
        patch("haniel.core.deployment_command_runner.subprocess.run") as run,
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
            "haniel.core.deployment_command_runner.subprocess.run",
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
            "haniel.core.deployment_command_runner.subprocess.run",
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
            "haniel.core.deployment_command_runner.subprocess.run",
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
