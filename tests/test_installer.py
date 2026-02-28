"""
Tests for the haniel installer module.

Tests cover:
- InstallState persistence and resumption
- MechanicalInstaller operations (directories, git, venv, npm)
- InteractiveInstaller MCP tools
- Finalizer operations (config generation, service registration)
- InstallOrchestrator flow control
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from haniel.config import (
    HanielConfig,
    InstallConfig,
    RepoConfig,
    EnvironmentConfig,
    ConfigFileConfig,
    ConfigKeyConfig,
    ServiceDefinitionConfig,
)


class TestInstallState:
    """Tests for InstallState model."""

    def test_create_new_state(self):
        """Test creating a new install state."""
        from haniel.installer.state import InstallState, InstallPhase

        state = InstallState()
        assert state.phase == InstallPhase.NOT_STARTED
        assert state.completed_steps == []
        assert state.failed_steps == []
        assert state.pending_configs == {}
        assert state.config_values == {}

    def test_save_and_load_state(self):
        """Test saving and loading install state."""
        from haniel.installer.state import InstallState, InstallPhase, StepStatus

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "install.state"

            # Create and save state
            state = InstallState(
                phase=InstallPhase.MECHANICAL,
                completed_steps=["directories", "repos"],
                failed_steps=[StepStatus(step="requirements", error="nssm not found")],
                config_values={"workspace-env": {"DEBUG": "false"}},
            )
            state.save(state_file)

            # Load state
            loaded = InstallState.load(state_file)
            assert loaded.phase == InstallPhase.MECHANICAL
            assert loaded.completed_steps == ["directories", "repos"]
            assert len(loaded.failed_steps) == 1
            assert loaded.failed_steps[0].step == "requirements"
            assert loaded.config_values == {"workspace-env": {"DEBUG": "false"}}

    def test_load_nonexistent_returns_new(self):
        """Test loading nonexistent state file returns new state."""
        from haniel.installer.state import InstallState, InstallPhase

        state = InstallState.load(Path("/nonexistent/install.state"))
        assert state.phase == InstallPhase.NOT_STARTED

    def test_mark_step_complete(self):
        """Test marking a step as complete."""
        from haniel.installer.state import InstallState

        state = InstallState()
        state.mark_complete("directories")
        assert "directories" in state.completed_steps

    def test_mark_step_failed(self):
        """Test marking a step as failed."""
        from haniel.installer.state import InstallState

        state = InstallState()
        state.mark_failed("requirements", "python not found")
        assert len(state.failed_steps) == 1
        assert state.failed_steps[0].step == "requirements"
        assert state.failed_steps[0].error == "python not found"


class TestMechanicalInstaller:
    """Tests for MechanicalInstaller."""

    @pytest.fixture
    def sample_config(self):
        """Create a sample config for testing."""
        return HanielConfig(
            install=InstallConfig(
                requirements={"python": ">=3.11", "node": ">=18"},
                directories=["./runtime", "./runtime/logs", "./workspace"],
                environments={
                    "main-venv": EnvironmentConfig(
                        type="python-venv",
                        path="./runtime/venv",
                        requirements=["./requirements.txt"],
                    ),
                },
                configs={
                    "static-config": ConfigFileConfig(
                        path="./config.json",
                        content='{"key": "value"}',
                    ),
                },
            ),
            repos={
                "test-repo": RepoConfig(
                    url="https://github.com/test/test.git",
                    branch="main",
                    path="./.projects/test",
                ),
            },
        )

    def test_check_requirements_python(self, sample_config):
        """Test checking Python requirement."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(sample_config, config_dir, state)

            # Python should be available
            results = installer.check_requirements()
            python_result = next(r for r in results if r["name"] == "python")
            assert python_result["installed"] is True

    def test_create_directories(self, sample_config):
        """Test creating directories."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(sample_config, config_dir, state)

            # Create directories
            installer.create_directories()

            # Check directories exist
            assert (config_dir / "runtime").exists()
            assert (config_dir / "runtime" / "logs").exists()
            assert (config_dir / "workspace").exists()

            # Check state updated
            assert "directories" in state.completed_steps

    def test_create_static_configs(self, sample_config):
        """Test creating static config files."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(sample_config, config_dir, state)

            # Create static configs
            installer.create_static_configs()

            # Check config file exists
            config_file = config_dir / "config.json"
            assert config_file.exists()
            content = json.loads(config_file.read_text())
            assert content == {"key": "value"}

    @patch("subprocess.run")
    def test_clone_repos_success(self, mock_run, sample_config):
        """Test cloning repositories."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        mock_run.return_value = MagicMock(returncode=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(sample_config, config_dir, state)

            # Clone repos
            installer.clone_repos()

            # Check git clone was called
            mock_run.assert_called()
            assert "repos" in state.completed_steps

    @patch("subprocess.run")
    def test_create_venv(self, mock_run, sample_config):
        """Test creating virtual environments."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        mock_run.return_value = MagicMock(returncode=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(sample_config, config_dir, state)

            # Create environments
            installer.create_environments()

            # Check venv creation was called
            assert mock_run.called


class TestInteractiveInstaller:
    """Tests for InteractiveInstaller MCP tools."""

    @pytest.fixture
    def interactive_config(self):
        """Create a config with interactive configs."""
        return HanielConfig(
            install=InstallConfig(
                configs={
                    "workspace-env": ConfigFileConfig(
                        path="./workspace/.env",
                        keys=[
                            ConfigKeyConfig(
                                key="SLACK_BOT_TOKEN",
                                prompt="Slack Bot Token",
                            ),
                            ConfigKeyConfig(
                                key="DEBUG",
                                default="false",
                            ),
                        ],
                    ),
                },
            ),
        )

    def test_get_install_status(self, interactive_config):
        """Test getting install status."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState, InstallPhase, StepStatus

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(
                phase=InstallPhase.INTERACTIVE,
                completed_steps=["directories", "repos"],
                failed_steps=[StepStatus(step="requirements", error="nssm not found")],
            )
            installer = InteractiveInstaller(interactive_config, config_dir, state)

            status = installer.get_install_status()

            assert status["phase"] == "interactive"
            assert "directories" in status["completed"]
            assert len(status["failed"]) == 1
            assert status["failed"][0]["step"] == "requirements"

    def test_set_config_value(self, interactive_config):
        """Test setting a config value."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = InteractiveInstaller(interactive_config, config_dir, state)

            result = installer.set_config("workspace-env", "SLACK_BOT_TOKEN", "xoxb-1234")

            assert result["success"] is True
            assert state.config_values["workspace-env"]["SLACK_BOT_TOKEN"] == "xoxb-1234"

    def test_get_config_status(self, interactive_config):
        """Test getting config status."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            state.config_values["workspace-env"] = {"DEBUG": "false"}
            installer = InteractiveInstaller(interactive_config, config_dir, state)

            status = installer.get_config("workspace-env")

            assert "DEBUG" in status["filled_keys"]
            assert "SLACK_BOT_TOKEN" in status["missing_keys"]

    def test_pending_configs_list(self, interactive_config):
        """Test getting pending configs list."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState, InstallPhase

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.INTERACTIVE)
            installer = InteractiveInstaller(interactive_config, config_dir, state)

            status = installer.get_install_status()

            assert len(status["pending_configs"]) == 1
            assert status["pending_configs"][0]["name"] == "workspace-env"
            assert "SLACK_BOT_TOKEN" in status["pending_configs"][0]["missing_keys"]


class TestFinalizer:
    """Tests for Finalizer."""

    @pytest.fixture
    def finalizer_config(self):
        """Create a config for finalization testing."""
        return HanielConfig(
            install=InstallConfig(
                configs={
                    "workspace-env": ConfigFileConfig(
                        path="./workspace/.env",
                        keys=[
                            ConfigKeyConfig(key="SLACK_TOKEN", prompt="Token"),
                            ConfigKeyConfig(key="DEBUG", default="false"),
                        ],
                    ),
                },
                service=ServiceDefinitionConfig(
                    name="haniel",
                    display="Haniel Service Runner",
                    working_directory="{root}",
                ),
            ),
        )

    def test_generate_env_file(self, finalizer_config):
        """Test generating .env file from collected values."""
        from haniel.installer.finalize import Finalizer
        from haniel.installer.state import InstallState, InstallPhase

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / "workspace").mkdir()

            state = InstallState(phase=InstallPhase.FINALIZE)
            state.config_values["workspace-env"] = {
                "SLACK_TOKEN": "xoxb-secret",
                "DEBUG": "true",
            }
            finalizer = Finalizer(finalizer_config, config_dir, state)

            finalizer.generate_config_files()

            env_file = config_dir / "workspace" / ".env"
            assert env_file.exists()
            content = env_file.read_text()
            assert "SLACK_TOKEN=xoxb-secret" in content
            assert "DEBUG=true" in content

    def test_check_all_configs_filled(self, finalizer_config):
        """Test checking if all required configs are filled."""
        from haniel.installer.finalize import Finalizer
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)

            # Missing required value
            state = InstallState()
            state.config_values["workspace-env"] = {"DEBUG": "false"}
            finalizer = Finalizer(finalizer_config, config_dir, state)

            assert finalizer.check_all_configs_filled() is False

            # All values filled
            state.config_values["workspace-env"]["SLACK_TOKEN"] = "xoxb-1234"
            assert finalizer.check_all_configs_filled() is True

    @patch("platform.system")
    @patch("shutil.which")
    @patch("subprocess.run")
    def test_register_nssm_service_windows(
        self, mock_run, mock_which, mock_system, finalizer_config
    ):
        """Test NSSM service registration on Windows."""
        from haniel.installer.finalize import Finalizer
        from haniel.installer.state import InstallState

        # Mock Windows environment
        mock_system.return_value = "Windows"
        mock_which.side_effect = lambda cmd: {
            "nssm": r"C:\tools\nssm.exe",
            "python": r"C:\Python312\python.exe",
        }.get(cmd)

        # Mock all subprocess calls to succeed
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            finalizer = Finalizer(finalizer_config, config_dir, state)

            finalizer.register_service()

            # Verify NSSM commands were called
            calls = mock_run.call_args_list
            # Should have called: remove (cleanup), install, set DisplayName,
            # set AppDirectory, set AppStdout, set AppStderr
            assert len(calls) >= 4

            # Check install command
            install_call = calls[1]  # Second call after remove
            assert "install" in install_call[0][0]
            assert "haniel" in install_call[0][0]

    @patch("platform.system")
    @patch("shutil.which")
    def test_register_nssm_service_nssm_not_found(
        self, mock_which, mock_system, finalizer_config
    ):
        """Test error when NSSM is not installed."""
        from haniel.installer.finalize import Finalizer
        from haniel.installer.state import InstallState

        mock_system.return_value = "Windows"
        mock_which.return_value = None

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            finalizer = Finalizer(finalizer_config, config_dir, state)

            with pytest.raises(RuntimeError, match="NSSM not found"):
                finalizer.register_service()

    @patch("platform.system")
    @patch("shutil.which")
    @patch("subprocess.run")
    def test_register_nssm_service_install_fails(
        self, mock_run, mock_which, mock_system, finalizer_config
    ):
        """Test error handling when NSSM install fails."""
        from haniel.installer.finalize import Finalizer
        from haniel.installer.state import InstallState

        mock_system.return_value = "Windows"
        mock_which.side_effect = lambda cmd: {
            "nssm": r"C:\tools\nssm.exe",
            "python": r"C:\Python312\python.exe",
        }.get(cmd)

        # First call (remove) succeeds, second (install) fails
        mock_run.side_effect = [
            MagicMock(returncode=0),  # remove
            MagicMock(returncode=1, stderr="Service already exists"),  # install fails
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            finalizer = Finalizer(finalizer_config, config_dir, state)

            with pytest.raises(RuntimeError, match="NSSM install failed"):
                finalizer.register_service()

    @patch("platform.system")
    def test_register_service_non_windows(self, mock_system, finalizer_config):
        """Test service registration logs instructions on non-Windows."""
        from haniel.installer.finalize import Finalizer
        from haniel.installer.state import InstallState

        mock_system.return_value = "Linux"

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            finalizer = Finalizer(finalizer_config, config_dir, state)

            # Should not raise, just log instructions
            finalizer.register_service()

    def test_register_service_no_service_config(self):
        """Test that register_service skips when no service is configured."""
        from haniel.installer.finalize import Finalizer
        from haniel.installer.state import InstallState

        config = HanielConfig(install=InstallConfig())  # No service config

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            finalizer = Finalizer(config, config_dir, state)

            # Should not raise
            finalizer.register_service()


class TestInstallOrchestrator:
    """Tests for InstallOrchestrator flow control."""

    @pytest.fixture
    def orchestrator_config(self):
        """Create a config for orchestration testing."""
        return HanielConfig(
            install=InstallConfig(
                requirements={"python": ">=3.11"},
                directories=["./runtime"],
                configs={
                    "test-config": ConfigFileConfig(
                        path="./config.json",
                        content='{"test": true}',
                    ),
                },
            ),
        )

    def test_phase_transition(self, orchestrator_config):
        """Test phase transitions."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState, InstallPhase

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            orchestrator = InstallOrchestrator(
                orchestrator_config, config_dir, state
            )

            # Initial state
            assert state.phase == InstallPhase.NOT_STARTED

            # After running mechanical phase
            orchestrator.run_mechanical_phase()
            assert state.phase in [InstallPhase.MECHANICAL, InstallPhase.INTERACTIVE]

    def test_resume_from_state(self, orchestrator_config):
        """Test resuming from saved state."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState, InstallPhase

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state_file = config_dir / "install.state"

            # Create and save state at INTERACTIVE phase
            state = InstallState(
                phase=InstallPhase.INTERACTIVE,
                completed_steps=["requirements", "directories", "repos"],
            )
            state.save(state_file)

            # Load and resume
            loaded_state = InstallState.load(state_file)
            orchestrator = InstallOrchestrator(
                orchestrator_config, config_dir, loaded_state
            )

            assert loaded_state.phase == InstallPhase.INTERACTIVE
            assert "directories" in loaded_state.completed_steps

    @patch("shutil.which")
    def test_check_claude_code(self, mock_which, orchestrator_config):
        """Test Claude Code availability check."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            orchestrator = InstallOrchestrator(
                orchestrator_config, config_dir, state
            )

            # Claude Code not installed
            mock_which.return_value = None
            assert orchestrator.check_claude_code() is False

            # Claude Code installed
            mock_which.return_value = "/usr/bin/claude"
            assert orchestrator.check_claude_code() is True


class TestInstallMcpServer:
    """Tests for InstallMcpServer."""

    @pytest.fixture
    def mcp_config(self):
        """Create a config with interactive configs for MCP testing."""
        return HanielConfig(
            install=InstallConfig(
                configs={
                    "workspace-env": ConfigFileConfig(
                        path="./workspace/.env",
                        keys=[
                            ConfigKeyConfig(
                                key="API_KEY",
                                prompt="API Key",
                                guide="Get it from https://example.com",
                            ),
                            ConfigKeyConfig(
                                key="DEBUG",
                                default="false",
                            ),
                        ],
                    ),
                },
            ),
        )

    def test_install_mcp_server_tools(self, mcp_config):
        """Test InstallMcpServer returns correct tools."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.install_mcp_server import InstallMcpServer
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = InteractiveInstaller(mcp_config, config_dir, state)
            server = InstallMcpServer(installer)

            tools = server.list_tools()

            # Check all expected tools are present
            tool_names = [t["name"] for t in tools]
            assert "haniel_install_status" in tool_names
            assert "haniel_set_config" in tool_names
            assert "haniel_get_config" in tool_names
            assert "haniel_retry_step" in tool_names
            assert "haniel_finalize_install" in tool_names

    @pytest.mark.asyncio
    async def test_install_mcp_server_call_tool(self, mcp_config):
        """Test calling tools through InstallMcpServer."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.install_mcp_server import InstallMcpServer
        from haniel.installer.state import InstallState, InstallPhase

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.INTERACTIVE)
            installer = InteractiveInstaller(mcp_config, config_dir, state)
            server = InstallMcpServer(installer)

            # Test haniel_install_status
            result = await server.call_tool("haniel_install_status", {})
            result_data = json.loads(result)
            assert result_data["phase"] == "interactive"

            # Test haniel_set_config
            result = await server.call_tool("haniel_set_config", {
                "config_name": "workspace-env",
                "key": "API_KEY",
                "value": "test-key-123",
            })
            result_data = json.loads(result)
            assert result_data["success"] is True

            # Verify value was set
            assert state.config_values["workspace-env"]["API_KEY"] == "test-key-123"

            # Test haniel_get_config
            result = await server.call_tool("haniel_get_config", {
                "config_name": "workspace-env",
            })
            result_data = json.loads(result)
            assert "API_KEY" in result_data["filled_keys"]

    @pytest.mark.asyncio
    async def test_install_mcp_server_finalize(self, mcp_config):
        """Test finalize flow through InstallMcpServer."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.install_mcp_server import InstallMcpServer
        from haniel.installer.state import InstallState, InstallPhase

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.INTERACTIVE)
            installer = InteractiveInstaller(mcp_config, config_dir, state)
            server = InstallMcpServer(installer)

            # Set required config
            await server.call_tool("haniel_set_config", {
                "config_name": "workspace-env",
                "key": "API_KEY",
                "value": "test-key-123",
            })

            # Finalize
            result = await server.call_tool("haniel_finalize_install", {})
            result_data = json.loads(result)
            assert result_data["success"] is True

            # Check state transitioned
            assert installer.is_finalize_requested()
            assert state.phase == InstallPhase.FINALIZE


class TestInteractiveInstallerClaudeSession:
    """Tests for InteractiveInstaller Claude Code session integration."""

    @pytest.fixture
    def session_config(self):
        """Create a config for session testing."""
        return HanielConfig(
            install=InstallConfig(
                configs={
                    "test-env": ConfigFileConfig(
                        path="./test/.env",
                        keys=[
                            ConfigKeyConfig(key="SECRET", prompt="Secret"),
                        ],
                    ),
                },
            ),
        )

    def test_get_claude_prompt(self, session_config):
        """Test Claude prompt generation."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState, InstallPhase

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.INTERACTIVE)
            installer = InteractiveInstaller(session_config, config_dir, state)

            prompt = installer.get_claude_prompt()

            # Check prompt contains essential elements
            assert "haniel" in prompt.lower()
            assert "haniel_install_status" in prompt
            assert "haniel_set_config" in prompt
            assert "haniel_finalize_install" in prompt
            assert "test-env" in prompt
            assert "SECRET" in prompt

    def test_get_install_mcp_port(self, session_config):
        """Test MCP port calculation."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState
        from haniel.config import McpConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()

            # Without MCP config
            installer = InteractiveInstaller(session_config, config_dir, state)
            assert installer._get_install_mcp_port() == 3201

            # With MCP config
            config_with_mcp = HanielConfig(
                mcp=McpConfig(enabled=True, port=3200),
                install=session_config.install,
            )
            installer = InteractiveInstaller(config_with_mcp, config_dir, state)
            assert installer._get_install_mcp_port() == 3201  # 3200 + 1

    @patch("haniel.installer.install_mcp_server.InstallMcpServer")
    @patch("haniel.installer.interactive.shutil.which")
    @patch("haniel.installer.interactive.subprocess.Popen")
    def test_launch_claude_code_session_success(
        self, mock_popen, mock_which, mock_server_class, session_config
    ):
        """Test launching Claude Code session successfully."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState, InstallPhase

        # Mock Claude Code executable
        mock_which.return_value = "/usr/bin/claude"

        # Mock subprocess - simulate Claude Code running and exiting
        mock_process = MagicMock()
        mock_process.wait.return_value = 0
        mock_popen.return_value = mock_process

        # Mock MCP server
        mock_server = MagicMock()
        mock_server_class.return_value = mock_server

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.INTERACTIVE)
            installer = InteractiveInstaller(session_config, config_dir, state)

            # Manually set finalize as if Claude Code called it
            installer._finalize_requested = True

            result = installer.launch_claude_code_session()

            # Should return True since finalize was "called"
            assert result is True

            # Check Claude Code was launched
            mock_popen.assert_called_once()
            call_args = mock_popen.call_args
            assert "/usr/bin/claude" in call_args[0][0]

            # Check MCP server was started and stopped
            mock_server.start_background.assert_called_once()
            mock_server.stop_background.assert_called_once()

    @patch("haniel.installer.install_mcp_server.InstallMcpServer")
    @patch("haniel.installer.interactive.shutil.which")
    def test_launch_claude_code_session_no_claude(
        self, mock_which, mock_server_class, session_config
    ):
        """Test launching session when Claude Code is not installed."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState, InstallPhase

        # Claude Code not installed
        mock_which.return_value = None

        # Mock MCP server
        mock_server = MagicMock()
        mock_server_class.return_value = mock_server

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.INTERACTIVE)
            installer = InteractiveInstaller(session_config, config_dir, state)

            result = installer.launch_claude_code_session()

            # Should return False
            assert result is False

            # MCP server should still be cleaned up
            mock_server.stop_background.assert_called_once()
