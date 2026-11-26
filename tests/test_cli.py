"""Tests for haniel CLI commands."""

import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import click
from click.testing import CliRunner

from haniel.cli import main
from haniel.core.lifecycle_control import LifecycleConflict


def test_httpx_logging_is_warning_even_in_debug(cli_runner, tmp_config):
    httpx_logger = logging.getLogger("httpx")
    original_level = httpx_logger.level
    root = logging.getLogger()
    handlers = set(root.handlers)
    try:
        httpx_logger.setLevel(logging.NOTSET)
        with patch(
            "haniel.core.runner.ServiceRunner", side_effect=RuntimeError("stop")
        ):
            cli_runner.invoke(main, ["run", str(tmp_config), "--log-level", "DEBUG"])
        assert httpx_logger.level == logging.WARNING
    finally:
        httpx_logger.setLevel(original_level)
        for handler in set(root.handlers) - handlers:
            root.removeHandler(handler)
            handler.close()


def test_active_self_head_comes_from_wrapper_environment(monkeypatch):
    from haniel.cli import _resolve_active_self_head

    commit = "a" * 40
    monkeypatch.setenv("HANIEL_ACTIVE_SELF_HEAD", commit.upper())

    assert _resolve_active_self_head(None) == commit


def test_active_self_head_rejects_conflicting_option_and_environment(monkeypatch):
    from haniel.cli import _resolve_active_self_head

    monkeypatch.setenv("HANIEL_ACTIVE_SELF_HEAD", "a" * 40)

    with pytest.raises(click.BadParameter, match="conflicts"):
        _resolve_active_self_head("b" * 40)


def test_self_update_exit_intent_is_atomic_and_immediate_exit_flushes(tmp_path):
    from haniel.cli import _exit_immediately
    from haniel.core.self_update_exit_intent import MARKER_RELPATH, write

    marker = write(tmp_path)

    assert marker == tmp_path / MARKER_RELPATH
    assert marker.is_file()
    assert not marker.with_suffix(marker.suffix + ".tmp").exists()

    stdout = MagicMock()
    stderr = MagicMock()
    with (
        patch.object(sys, "stdout", stdout),
        patch.object(sys, "stderr", stderr),
        patch.object(os, "_exit", side_effect=RuntimeError("exit")) as hard_exit,
    ):
        with pytest.raises(RuntimeError, match="exit"):
            _exit_immediately(10)

    stdout.flush.assert_called_once_with()
    stderr.flush.assert_called_once_with()
    hard_exit.assert_called_once_with(10)


class TestCLIBasics:
    """Test basic CLI functionality."""

    def test_main_without_arguments_shows_help(self, cli_runner: CliRunner):
        """Running haniel without arguments should show help."""
        result = cli_runner.invoke(main, [])
        assert result.exit_code == 0
        assert "Usage:" in result.output
        assert "haniel" in result.output.lower()

    def test_help_flag(self, cli_runner: CliRunner):
        """--help should display usage information."""
        result = cli_runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Usage:" in result.output
        assert "install" in result.output
        assert "run" in result.output
        assert "status" in result.output
        assert "validate" in result.output

    def test_internal_commands_are_hidden_but_still_work(self, cli_runner: CliRunner):
        """handover and lifecycle are for haniel itself, not for users."""
        help_text = cli_runner.invoke(main, ["--help"]).output
        assert "handover" not in help_text
        assert "lifecycle" not in help_text
        assert cli_runner.invoke(main, ["lifecycle", "--help"]).exit_code == 0
        assert cli_runner.invoke(main, ["handover", "--help"]).exit_code == 0

    def test_version_flag(self, cli_runner: CliRunner):
        """--version should display version information."""
        result = cli_runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output


class TestInstallCommand:
    """Test the install command."""

    def test_install_help(self, cli_runner: CliRunner):
        """install --help should show install-specific help."""
        result = cli_runner.invoke(main, ["install", "--help"])
        assert result.exit_code == 0
        assert "install" in result.output.lower()
        assert "--state-policy" in result.output
        assert "--defer-manifest-handover" in result.output

    def test_noninteractive_existing_state_never_prompts_without_policy(
        self, cli_runner: CliRunner, tmp_config
    ):
        from haniel.installer.state import InstallState

        InstallState().save(tmp_config.parent / "install.state")
        with patch("haniel.commands.install.click.confirm") as confirm:
            result = cli_runner.invoke(
                main,
                ["install", "--skip-interactive", str(tmp_config)],
            )

        assert result.exit_code == 1
        assert "STATE_POLICY_REQUIRED" in result.output
        confirm.assert_not_called()

    def test_install_requires_config(self, cli_runner: CliRunner):
        """install without config should show error or help."""
        result = cli_runner.invoke(main, ["install"])
        # Should either show help or require a config file
        assert result.exit_code in [0, 2]  # 0 = help, 2 = missing arg

    def test_install_with_nonexistent_config(self, cli_runner: CliRunner):
        """install with nonexistent config should show error."""
        result = cli_runner.invoke(main, ["install", "nonexistent.yaml"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower() or "error" in result.output.lower()

    @patch("shutil.which", return_value="/usr/bin/claude")
    def test_install_with_valid_config(
        self, mock_which, cli_runner: CliRunner, tmp_config
    ):
        """install with valid config should succeed (skeleton)."""
        result = cli_runner.invoke(main, ["install", str(tmp_config)])
        # Skeleton just acknowledges the command
        assert result.exit_code == 0


class TestHandoverCommand:
    def test_json_failure_is_one_bounded_redacted_stable_envelope(
        self, cli_runner: CliRunner, tmp_config
    ) -> None:
        secret = "super-secret-value"
        with patch(
            "haniel.commands.handover.execute_manifest_handover_once",
            side_effect=LifecycleConflict(
                "LIFECYCLE_OWNER_MISSING",
                f"TOKEN={secret} " + ("x" * 10000),
            ),
        ):
            result = cli_runner.invoke(
                main,
                [
                    "handover",
                    str(tmp_config),
                    "app",
                    "--target-ref",
                    "origin/main",
                    "--expected-operation",
                    "fresh_install",
                    "--request-id",
                    "request-1",
                    "--json",
                ],
            )

        assert result.exit_code == 1
        assert len(result.output.splitlines()) == 1
        payload = json.loads(result.output)
        assert payload["schema_version"] == "haniel.handover.result.v1"
        assert payload["error"]["code"] == "LIFECYCLE_OWNER_MISSING"
        assert secret not in result.output
        assert "[REDACTED]" in payload["error"]["message"]
        assert len(payload["error"]["message"]) <= 4096


class TestRunCommand:
    """Test the run command."""

    def test_run_help(self, cli_runner: CliRunner):
        """run --help should show run-specific help."""
        result = cli_runner.invoke(main, ["run", "--help"])
        assert result.exit_code == 0
        assert "run" in result.output.lower()

    def test_run_without_config_and_without_default_file_exits_2(
        self, cli_runner: CliRunner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(main, ["run"])
        assert result.exit_code == 2
        assert "./haniel.yaml was not found" in result.output
        assert "Traceback" not in result.output

    def test_run_without_config_uses_default_file(
        self, cli_runner: CliRunner, tmp_config, monkeypatch
    ):
        monkeypatch.chdir(tmp_config.parent)
        result = cli_runner.invoke(main, ["run", "--dry-run"])
        assert result.exit_code == 0
        assert "[dry-run] Configuration: haniel.yaml" in result.output

    def test_foreground_flag_is_accepted_but_hidden(
        self, cli_runner: CliRunner, tmp_config
    ):
        """one_shot_handover still passes --foreground to the new version."""
        assert "--foreground" not in cli_runner.invoke(main, ["run", "--help"]).output
        result = cli_runner.invoke(
            main, ["run", "-f", "--foreground", "--dry-run", str(tmp_config)]
        )
        assert result.exit_code == 0

    def test_second_run_for_same_config_says_already_running(
        self, cli_runner: CliRunner, tmp_config
    ):
        from haniel.core.lifecycle_control import LifecycleControl

        owner = LifecycleControl(tmp_config.resolve()).acquire_owner("first")
        try:
            result = cli_runner.invoke(main, ["run", str(tmp_config)])
        finally:
            owner.__exit__(None, None, None)

        assert result.exit_code == 1
        assert "haniel is already running with this config" in result.output
        assert str(tmp_config.resolve()) in result.output
        assert f"pid {os.getpid()}" in result.output
        assert "Traceback" not in result.output
        assert len(result.output.strip().splitlines()) == 1

    def test_run_with_nonexistent_config(self, cli_runner: CliRunner):
        """run with nonexistent config should show error."""
        result = cli_runner.invoke(main, ["run", "nonexistent.yaml"])
        assert result.exit_code != 0

    def test_run_with_valid_config_dry_run(self, cli_runner: CliRunner, tmp_config):
        """run --dry-run with valid config should succeed."""
        result = cli_runner.invoke(main, ["run", "--dry-run", str(tmp_config)])
        assert result.exit_code == 0
        assert "dry-run" in result.output.lower()


class TestStatusCommand:
    """Test the status command."""

    def test_status_help(self, cli_runner: CliRunner):
        """status --help should show status-specific help."""
        result = cli_runner.invoke(main, ["status", "--help"])
        assert result.exit_code == 0

    def test_status_without_config(self, cli_runner: CliRunner):
        """status without config should work (shows not running)."""
        result = cli_runner.invoke(main, ["status"])
        assert result.exit_code == 0


class TestValidateCommand:
    """Test the validate command."""

    def test_validate_help(self, cli_runner: CliRunner):
        """validate --help should show validate-specific help."""
        result = cli_runner.invoke(main, ["validate", "--help"])
        assert result.exit_code == 0

    def test_validate_requires_config(self, cli_runner: CliRunner):
        """validate without config should show error or help."""
        result = cli_runner.invoke(main, ["validate"])
        assert result.exit_code in [0, 2]

    def test_validate_with_nonexistent_config(self, cli_runner: CliRunner):
        """validate with nonexistent config should show error."""
        result = cli_runner.invoke(main, ["validate", "nonexistent.yaml"])
        assert result.exit_code != 0

    def test_validate_with_valid_config(self, cli_runner: CliRunner, tmp_config):
        """validate with valid config should succeed."""
        result = cli_runner.invoke(main, ["validate", str(tmp_config)])
        assert result.exit_code == 0
        assert "valid" in result.output.lower() or "ok" in result.output.lower()

    def test_validate_with_invalid_yaml(self, cli_runner: CliRunner, tmp_path):
        """validate with invalid YAML should fail."""
        invalid_yaml = tmp_path / "invalid.yaml"
        invalid_yaml.write_text("invalid: yaml: syntax: [")
        result = cli_runner.invoke(main, ["validate", str(invalid_yaml)])
        assert result.exit_code != 0


class TestInstallDryRun:
    """Test install --dry-run functionality."""

    def test_install_dry_run(self, cli_runner: CliRunner, tmp_config):
        """install --dry-run should show what would be done."""
        result = cli_runner.invoke(main, ["install", "--dry-run", str(tmp_config)])
        assert result.exit_code == 0
        assert "dry-run" in result.output.lower()


class TestRunForeground:
    """Test run --foreground functionality."""

    def test_run_foreground_dry_run(self, cli_runner: CliRunner, tmp_config):
        """run --foreground --dry-run should show dry run output."""
        result = cli_runner.invoke(
            main, ["run", "--foreground", "--dry-run", str(tmp_config)]
        )
        assert result.exit_code == 0
        assert "dry-run" in result.output.lower()


class TestStatusJson:
    """Test status --json functionality."""

    def test_status_json(self, cli_runner: CliRunner):
        """status --json should output JSON."""
        result = cli_runner.invoke(main, ["status", "--json"])
        assert result.exit_code == 0
        assert "{" in result.output
        assert "running" in result.output

    def test_status_with_config(self, cli_runner: CliRunner, tmp_config):
        """status with config should show status details."""
        result = cli_runner.invoke(main, ["status", str(tmp_config)])
        assert result.exit_code == 0
        # Should show status info
        assert "Status:" in result.output


class TestModuleExecution:
    """Test running haniel as a module."""

    def test_module_import(self):
        """haniel module should be importable."""
        import haniel

        assert hasattr(haniel, "__version__")
        assert haniel.__version__ == "0.1.0"

    def test_main_import(self):
        """haniel.__main__ should import main from cli."""
        from haniel.__main__ import main as main_func
        from haniel.cli import main as cli_main

        assert main_func is cli_main


class TestValidateWithRealFixtures:
    """Test validate command with actual fixture files."""

    FIXTURES_DIR = Path(__file__).parent / "fixtures"

    def test_validate_valid_config_shows_summary(self, cli_runner: CliRunner):
        """validate with valid config shows detailed summary."""
        result = cli_runner.invoke(
            main, ["validate", str(self.FIXTURES_DIR / "valid_config.yaml")]
        )
        assert result.exit_code == 0
        assert "Validation passed" in result.output
        assert "Poll interval: 60s" in result.output
        assert "Repositories: 2" in result.output
        assert "Services: 2" in result.output

    def test_validate_circular_dependency(self, cli_runner: CliRunner):
        """validate detects circular dependencies."""
        result = cli_runner.invoke(
            main, ["validate", str(self.FIXTURES_DIR / "invalid_circular.yaml")]
        )
        assert result.exit_code != 0
        assert "Validation FAILED" in result.output
        assert "Circular dependency" in result.output

    def test_validate_port_conflict(self, cli_runner: CliRunner):
        """validate detects port conflicts."""
        result = cli_runner.invoke(
            main, ["validate", str(self.FIXTURES_DIR / "invalid_port_conflict.yaml")]
        )
        assert result.exit_code != 0
        assert "Validation FAILED" in result.output
        assert "3100" in result.output

    def test_validate_duplicate_paths(self, cli_runner: CliRunner):
        """validate detects duplicate repository paths."""
        result = cli_runner.invoke(
            main, ["validate", str(self.FIXTURES_DIR / "invalid_path_duplicate.yaml")]
        )
        assert result.exit_code != 0
        assert "Validation FAILED" in result.output
        assert "Duplicate" in result.output or "path" in result.output.lower()

    def test_validate_missing_after(self, cli_runner: CliRunner):
        """validate detects missing after references."""
        result = cli_runner.invoke(
            main, ["validate", str(self.FIXTURES_DIR / "invalid_missing_after.yaml")]
        )
        assert result.exit_code != 0
        assert "Validation FAILED" in result.output
        assert "non-existent-service" in result.output

    def test_validate_missing_repo(self, cli_runner: CliRunner):
        """validate detects missing repo references."""
        result = cli_runner.invoke(
            main, ["validate", str(self.FIXTURES_DIR / "invalid_missing_repo.yaml")]
        )
        assert result.exit_code != 0
        assert "Validation FAILED" in result.output
        assert "non-existent-repo" in result.output


class TestInstallDryRunDetailed:
    """Test install --dry-run with various configurations."""

    FIXTURES_DIR = Path(__file__).parent / "fixtures"

    def test_install_dry_run_shows_phases(self, cli_runner: CliRunner):
        """install --dry-run shows all three phases."""
        result = cli_runner.invoke(
            main, ["install", "--dry-run", str(self.FIXTURES_DIR / "valid_config.yaml")]
        )
        assert result.exit_code == 0
        assert "Phase 1" in result.output
        assert "Phase 2" in result.output
        assert "Phase 3" in result.output

    def test_install_dry_run_shows_requirements(self, cli_runner: CliRunner):
        """install --dry-run shows requirements."""
        result = cli_runner.invoke(
            main, ["install", "--dry-run", str(self.FIXTURES_DIR / "valid_config.yaml")]
        )
        assert result.exit_code == 0
        assert "python" in result.output
        assert ">=3.11" in result.output

    def test_install_dry_run_shows_directories(self, cli_runner: CliRunner):
        """install --dry-run shows directories to create."""
        result = cli_runner.invoke(
            main, ["install", "--dry-run", str(self.FIXTURES_DIR / "valid_config.yaml")]
        )
        assert result.exit_code == 0
        assert "./runtime" in result.output
        assert "./runtime/logs" in result.output

    def test_install_dry_run_shows_repos(self, cli_runner: CliRunner):
        """install --dry-run shows repositories to clone."""
        result = cli_runner.invoke(
            main, ["install", "--dry-run", str(self.FIXTURES_DIR / "valid_config.yaml")]
        )
        assert result.exit_code == 0
        assert "test-repo" in result.output
        assert "another-repo" in result.output

    def test_install_dry_run_shows_service(self, cli_runner: CliRunner):
        """install --dry-run shows service registration."""
        result = cli_runner.invoke(
            main, ["install", "--dry-run", str(self.FIXTURES_DIR / "valid_config.yaml")]
        )
        assert result.exit_code == 0
        assert "haniel" in result.output
        assert "Haniel Service Runner" in result.output

    def test_install_with_invalid_config_fails(self, cli_runner: CliRunner):
        """install with invalid config should fail."""
        result = cli_runner.invoke(
            main, ["install", str(self.FIXTURES_DIR / "invalid_circular.yaml")]
        )
        assert result.exit_code != 0
        assert "Configuration errors" in result.output


class TestRunDryRunDetailed:
    """Test run --dry-run with various configurations."""

    FIXTURES_DIR = Path(__file__).parent / "fixtures"

    def test_run_dry_run_shows_poll_interval(self, cli_runner: CliRunner):
        """run --dry-run shows poll interval."""
        result = cli_runner.invoke(
            main, ["run", "--dry-run", str(self.FIXTURES_DIR / "valid_config.yaml")]
        )
        assert result.exit_code == 0
        assert "Poll interval: 60s" in result.output

    def test_run_dry_run_shows_repos(self, cli_runner: CliRunner):
        """run --dry-run shows repositories to monitor."""
        result = cli_runner.invoke(
            main, ["run", "--dry-run", str(self.FIXTURES_DIR / "valid_config.yaml")]
        )
        assert result.exit_code == 0
        assert "test-repo" in result.output
        assert "main" in result.output

    def test_run_dry_run_shows_services(self, cli_runner: CliRunner):
        """run --dry-run shows services in startup order."""
        result = cli_runner.invoke(
            main, ["run", "--dry-run", str(self.FIXTURES_DIR / "valid_config.yaml")]
        )
        assert result.exit_code == 0
        assert "mcp-server" in result.output
        assert "main-app" in result.output
        # main-app depends on mcp-server
        assert "after: mcp-server" in result.output

    def test_run_dry_run_shows_ready_conditions(self, cli_runner: CliRunner):
        """run --dry-run shows ready conditions."""
        result = cli_runner.invoke(
            main, ["run", "--dry-run", str(self.FIXTURES_DIR / "valid_config.yaml")]
        )
        assert result.exit_code == 0
        assert "ready: port:3100" in result.output

    def test_run_with_invalid_config_fails(self, cli_runner: CliRunner):
        """run with invalid config should fail."""
        result = cli_runner.invoke(
            main, ["run", "--dry-run", str(self.FIXTURES_DIR / "invalid_circular.yaml")]
        )
        assert result.exit_code != 0
        assert "Configuration errors" in result.output


class TestInstallerExpectations:
    """Install command guidance and Claude Code requirement (card 1896)."""

    @staticmethod
    def _write_config(directory, *, key_lines: str = "", service: bool = False):
        install = ""
        if key_lines:
            install += (
                "  configs:\n    app-env:\n      path: ./app.env\n      keys:\n"
                f"{key_lines}"
            )
        if service:
            install += "  service:\n    name: haniel\n    working_directory: '{root}'\n"
        config = directory / "haniel.yaml"
        config.write_text(
            "repos: {}\nservices: {}\n" + (f"install:\n{install}" if install else "")
        )
        return config

    @pytest.mark.parametrize(
        ("system", "expected"),
        [
            ("Linux", "Start with: sudo systemctl start haniel"),
            ("Windows", "Start with: sc start haniel"),
            ("Darwin", "Start with: launchctl bootstrap gui/"),
        ],
    )
    def test_completion_start_command_matches_the_os(
        self, system, expected, tmp_path, capsys
    ):
        from haniel.commands.install import _print_completion
        from haniel.config import HanielConfig

        config = HanielConfig.model_validate(
            {"install": {"service": {"name": "haniel"}}}
        )
        orchestrator = MagicMock()
        orchestrator.state.is_step_complete.return_value = False
        orchestrator.finalizer.get_completion_summary.return_value = {
            "generated_files": [],
            "service": {"name": "haniel"},
        }

        with (
            patch("haniel.commands.install.platform.system", return_value=system),
            patch("haniel.installer.launchd.os.getuid", create=True, return_value=501),
        ):
            _print_completion(orchestrator, tmp_path / "haniel.yaml", config)

        output = capsys.readouterr().out
        assert expected in output
        if system != "Windows":
            assert "sc start" not in output
        if system == "Darwin":
            assert "com.haniel.haniel.plist" in output

    def test_skip_interactive_installs_without_claude_code(
        self, cli_runner: CliRunner, tmp_path
    ):
        config = self._write_config(
            tmp_path, key_lines="        - key: TOKEN\n          default: abc\n"
        )

        with patch("haniel.installer.orchestrator.find_tool", return_value=None):
            result = cli_runner.invoke(
                main, ["install", "--skip-interactive", str(config)]
            )

        assert result.exit_code == 0, result.output
        assert "Installation Complete" in result.output
        assert (tmp_path / "app.env").read_text() == "TOKEN=abc\n"

    def test_interactive_install_without_pending_keys_does_not_need_claude_code(
        self, cli_runner: CliRunner, tmp_path
    ):
        config = self._write_config(
            tmp_path, key_lines="        - key: TOKEN\n          default: abc\n"
        )

        with (
            patch("haniel.installer.orchestrator.find_tool", return_value=None),
            patch("haniel.commands.install.os.isatty", return_value=True),
        ):
            result = cli_runner.invoke(main, ["install", str(config)])

        assert result.exit_code == 0, result.output

    def test_interactive_install_with_pending_keys_stops_without_claude_code(
        self, cli_runner: CliRunner, tmp_path
    ):
        config = self._write_config(tmp_path, key_lines="        - key: TOKEN\n")

        with (
            patch("haniel.installer.orchestrator.find_tool", return_value=None),
            patch("haniel.commands.install.os.isatty", return_value=True),
        ):
            result = cli_runner.invoke(main, ["install", str(config)])

        assert result.exit_code == 1
        assert "--skip-interactive" in result.output

    @pytest.mark.skipif(
        sys.platform == "win32", reason="validate commands run through sh"
    )
    def test_rejected_default_stops_install_and_reports_key_command_and_output(
        self, cli_runner: CliRunner, tmp_path
    ):
        config = self._write_config(
            tmp_path,
            key_lines=(
                "        - key: TOKEN\n          default: abc\n"
                '          validate: "echo token-rejected >&2; exit 4"\n'
            ),
        )

        result = cli_runner.invoke(main, ["install", "--skip-interactive", str(config)])

        assert result.exit_code == 1
        assert "app-env.TOKEN" in result.output
        assert "echo token-rejected" in result.output
        assert "exit code 4" in result.output
        assert "token-rejected" in result.output
        assert not (tmp_path / "app.env").exists()


class TestStatusReportsRuntime:
    """status must not call a running haniel Stopped."""

    @pytest.fixture
    def web_config(self, tmp_path: Path) -> Path:
        config_file = tmp_path / "haniel.yaml"
        config_file.write_text(
            "poll_interval: 60\n"
            "mcp: {enabled: true, port: 3299}\n"
            "dashboard: {enabled: true, token: secret-token}\n"
            "repos: {}\n"
            "services:\n"
            "  api: {run: python api.py}\n"
            "  old: {run: python old.py, enabled: false}\n",
            encoding="utf-8",
        )
        return config_file

    @pytest.fixture
    def running_owner(self, web_config: Path):
        from haniel.core.lifecycle_control import LifecycleControl

        owner = LifecycleControl(web_config.resolve()).acquire_owner("resident")
        yield owner
        owner.__exit__(None, None, None)

    def test_not_running_says_so_and_lists_config_services(
        self, cli_runner: CliRunner, web_config: Path
    ):
        result = cli_runner.invoke(main, ["status", str(web_config)])

        assert result.exit_code == 0
        assert "Not running" in result.output
        assert "no running haniel owns this config" in result.output
        assert "Services (from config; not runtime state):" in result.output
        assert "api: enabled" in result.output
        assert "old: disabled" in result.output
        assert "stopped" not in result.output.lower()

    def test_running_shows_pid_and_asks_the_dashboard_with_the_token(
        self, cli_runner: CliRunner, web_config: Path, running_owner
    ):
        runtime = {
            "running": True,
            "poll_interval": 60,
            "services": {"api": {"state": "ready", "uptime": 12, "restart_count": 0}},
            "repos": {},
        }
        response = MagicMock(status_code=200)
        response.json.return_value = runtime
        with patch("haniel.commands.status.httpx.get", return_value=response) as get:
            result = cli_runner.invoke(main, ["status", str(web_config)])

        assert result.exit_code == 0
        assert f"Running (pid {os.getpid()})" in result.output
        assert "api: ready (uptime: 12s)" in result.output
        assert "from config" not in result.output
        assert get.call_args.args == ("http://127.0.0.1:3299/api/status",)
        assert get.call_args.kwargs["headers"] == {
            "Authorization": "Bearer secret-token"
        }

    def test_running_but_unreachable_gives_the_reason_not_stopped(
        self, cli_runner: CliRunner, web_config: Path, running_owner
    ):
        import httpx

        with patch(
            "haniel.commands.status.httpx.get", side_effect=httpx.ConnectError("no")
        ):
            result = cli_runner.invoke(main, ["status", str(web_config)])

        assert result.exit_code == 0
        assert f"Running (pid {os.getpid()})" in result.output
        assert "could not reach running haniel at 127.0.0.1:3299" in result.output
        assert "Services (from config; not runtime state):" in result.output
        assert "stopped" not in result.output.lower()

    def test_rejected_token_is_reported(
        self, cli_runner: CliRunner, web_config: Path, running_owner
    ):
        with patch(
            "haniel.commands.status.httpx.get",
            return_value=MagicMock(status_code=401),
        ):
            result = cli_runner.invoke(main, ["status", str(web_config)])

        assert "rejected the dashboard token (HTTP 401)" in result.output

    def test_dashboard_disabled_says_runtime_status_is_unavailable(
        self, cli_runner: CliRunner, tmp_path: Path
    ):
        from haniel.core.lifecycle_control import LifecycleControl

        config = tmp_path / "haniel.yaml"
        config.write_text(
            "mcp: {enabled: true}\nrepos: {}\nservices:\n  api: {run: python api.py}\n",
            encoding="utf-8",
        )
        owner = LifecycleControl(config.resolve()).acquire_owner("resident")
        try:
            with patch("haniel.commands.status.httpx.get") as get:
                result = cli_runner.invoke(main, ["status", str(config)])
        finally:
            owner.__exit__(None, None, None)

        get.assert_not_called()
        assert "dashboard disabled so runtime status is unavailable" in result.output
        assert "stopped" not in result.output.lower()

    def test_json_marks_config_only_answers(
        self, cli_runner: CliRunner, web_config: Path
    ):
        result = cli_runner.invoke(main, ["status", "--json", str(web_config)])

        data = json.loads(result.output)
        assert data["running"] is False
        assert data["source"] == "config"
        assert data["runtime_status"] == "unavailable"
        assert data["services"]["old"] == {"enabled": False, "run": "python old.py"}


class TestValidateInputChecks:
    """validate rejects misspelled keys and unusable values; run only warns."""

    def test_validate_rejects_top_level_typo(self, cli_runner: CliRunner, tmp_path):
        config = tmp_path / "haniel.yaml"
        config.write_text("serivces: {}\n", encoding="utf-8")
        result = cli_runner.invoke(main, ["validate", str(config)])
        assert result.exit_code == 1
        assert "unknown key serivces (did you mean services?)" in result.output
        assert "no services are defined" in result.output

    def test_validate_lists_every_problem(self, cli_runner: CliRunner, tmp_path):
        config = tmp_path / "haniel.yaml"
        config.write_text(
            "shutdown: {signal: SIGTRM}\n"
            "services:\n  api:\n    run: x\n    redy: 'port:80'\n"
            "    ready: 'prt:80'\n    restart_delay: -5\n",
            encoding="utf-8",
        )
        result = cli_runner.invoke(main, ["validate", str(config)])
        assert result.exit_code == 1
        assert "unknown key services.api.redy (did you mean ready?)" in result.output
        assert "unknown signal 'SIGTRM'" in result.output
        assert "services.api.ready" in result.output
        assert "services.api.restart_delay" in result.output

    def test_validate_reports_unknown_keys_when_the_schema_fails(
        self, cli_runner: CliRunner, tmp_path
    ):
        config = tmp_path / "haniel.yaml"
        config.write_text("services:\n  api:\n    rn: x\n", encoding="utf-8")
        result = cli_runner.invoke(main, ["validate", str(config)])
        assert result.exit_code == 1
        assert "unknown key services.api.rn (did you mean run?)" in result.output

    def test_zero_services_warns_and_passes(self, cli_runner: CliRunner, tmp_config):
        result = cli_runner.invoke(main, ["validate", str(tmp_config)])
        assert result.exit_code == 0
        assert "no services are defined" in result.output

    def test_run_warns_and_still_starts(
        self, cli_runner: CliRunner, tmp_path, caplog: pytest.LogCaptureFixture
    ):
        config = tmp_path / "haniel.yaml"
        config.write_text(
            "services:\n  api:\n    run: x\n    redy: 'port:80'\n    ready: 'prt:80'\n",
            encoding="utf-8",
        )
        root = logging.getLogger()
        handlers = list(root.handlers)
        try:
            with patch(
                "haniel.core.runner.ServiceRunner", side_effect=RuntimeError("stop")
            ):
                result = cli_runner.invoke(main, ["run", str(config)])
        finally:
            for handler in list(root.handlers):
                if handler not in handlers:
                    root.removeHandler(handler)
                    handler.close()
        assert "Configuration errors" not in result.output
        assert "Starting haniel with config" in result.output
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("unknown key services.api.redy" in w for w in warnings)
        assert any("services.api.ready" in w for w in warnings)
