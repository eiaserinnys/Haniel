"""Tests for haniel CLI commands."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from haniel.cli import main
from haniel.core.lifecycle_control import LifecycleConflict


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
        assert "handover" in result.output
        assert "lifecycle" in result.output

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

    def test_run_requires_config(self, cli_runner: CliRunner):
        """run without config should show error or help."""
        result = cli_runner.invoke(main, ["run"])
        assert result.exit_code in [0, 2]

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
