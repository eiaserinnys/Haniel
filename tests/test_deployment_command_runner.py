"""Release manifest command executable resolution tests."""

import os
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
        patch("haniel.core.deployment_command_runner.subprocess.run") as run,
    ):
        runner(
            CommandSpec(name="verify-board-yjs", command="pnpm --version"),
            {"PATH": str(tmp_path), "PATHEXT": ".COM;.EXE;.BAT;.CMD"},
        )

    which.assert_called_once_with("pnpm", path=str(tmp_path))
    assert run.call_args.args[0] == [resolved, "--version"]


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
