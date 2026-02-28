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


class TestInstallMcpServerBackground:
    """Tests for InstallMcpServer background execution."""

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
                            ),
                        ],
                    ),
                },
            ),
        )

    def test_install_mcp_server_is_running(self, mcp_config):
        """Test InstallMcpServer is_running method."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.install_mcp_server import InstallMcpServer
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = InteractiveInstaller(mcp_config, config_dir, state)
            server = InstallMcpServer(installer)

            # Initially not running
            assert server.is_running() is False

    @pytest.mark.asyncio
    async def test_install_mcp_server_stop(self, mcp_config):
        """Test InstallMcpServer stop method."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.install_mcp_server import InstallMcpServer
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = InteractiveInstaller(mcp_config, config_dir, state)
            server = InstallMcpServer(installer)

            # Call stop without starting - should not raise
            await server.stop()


class TestOrchestratorExtended:
    """Extended tests for InstallOrchestrator."""

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
                    "test-env": ConfigFileConfig(
                        path="./.env",
                        keys=[
                            ConfigKeyConfig(key="TEST_KEY", prompt="Test Key"),
                        ],
                    ),
                },
            ),
        )

    def test_save_state(self, orchestrator_config):
        """Test saving state to file."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState, InstallPhase

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.MECHANICAL)
            orchestrator = InstallOrchestrator(
                orchestrator_config, config_dir, state
            )

            orchestrator.save_state()

            state_file = config_dir / "install.state"
            assert state_file.exists()

    @patch("shutil.which")
    def test_run_bootstrap_phase_success(self, mock_which, orchestrator_config):
        """Test successful bootstrap phase."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState, InstallPhase

        mock_which.return_value = "/usr/bin/claude"

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            orchestrator = InstallOrchestrator(
                orchestrator_config, config_dir, state
            )

            result = orchestrator.run_bootstrap_phase()

            assert result is True
            assert state.phase == InstallPhase.MECHANICAL

    @patch("shutil.which")
    def test_run_bootstrap_phase_no_claude(self, mock_which, orchestrator_config):
        """Test bootstrap phase without Claude Code."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState

        mock_which.return_value = None

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            orchestrator = InstallOrchestrator(
                orchestrator_config, config_dir, state
            )

            result = orchestrator.run_bootstrap_phase()

            assert result is False

    def test_retry_step_directories(self, orchestrator_config):
        """Test retrying directories step."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState, InstallPhase

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.MECHANICAL)
            orchestrator = InstallOrchestrator(
                orchestrator_config, config_dir, state
            )

            result = orchestrator.retry_step("directories")

            assert result["success"] is True
            assert (config_dir / "runtime").exists()

    def test_retry_step_unknown(self, orchestrator_config):
        """Test retrying unknown step."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            orchestrator = InstallOrchestrator(
                orchestrator_config, config_dir, state
            )

            result = orchestrator.retry_step("unknown_step")

            assert result["success"] is False
            assert "Unknown step" in result["error"]


class TestMechanicalInstallerExtended:
    """Extended tests for MechanicalInstaller."""

    @pytest.fixture
    def sample_config(self):
        """Create a sample config for testing."""
        return HanielConfig(
            install=InstallConfig(
                requirements={"python": ">=3.11", "node": ">=18", "nssm": True, "claude-code": True},
                directories=["./runtime", "./runtime/logs"],
                environments={
                    "main-venv": EnvironmentConfig(
                        type="python-venv",
                        path="./runtime/venv",
                        requirements=["./requirements.txt"],
                    ),
                    "npm-env": EnvironmentConfig(
                        type="npm",
                        path="./runtime/npm-project",
                    ),
                },
                configs={
                    "static-config": ConfigFileConfig(
                        path="./config.json",
                        content='{"root": "{root}"}',
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

    def test_resolve_path_absolute(self, sample_config):
        """Test resolving absolute path."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(sample_config, config_dir, state)

            result = installer._resolve_path("/absolute/path")
            assert result == Path("/absolute/path")

    def test_check_version_greater_equal(self, sample_config):
        """Test version checking with >=."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(sample_config, config_dir, state)

            passes, msg = installer._check_version("3.12.0", ">=3.11")
            assert passes is True

            passes, msg = installer._check_version("3.10.0", ">=3.11")
            assert passes is False

    def test_check_version_operators(self, sample_config):
        """Test various version operators."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(sample_config, config_dir, state)

            # Test > (use full version format for comparison)
            passes, _ = installer._check_version("3.12.0", ">3.11.0")
            assert passes is True

            # Test ==
            passes, _ = installer._check_version("3.11.0", "==3.11.0")
            assert passes is True

            # Test <=
            passes, _ = installer._check_version("3.11.0", "<=3.12.0")
            assert passes is True

            # Test <
            passes, _ = installer._check_version("3.10.0", "<3.11.0")
            assert passes is True

    def test_check_version_invalid(self, sample_config):
        """Test invalid version format."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(sample_config, config_dir, state)

            passes, _ = installer._check_version("invalid", ">=3.11")
            assert passes is False

    @patch("platform.system")
    @patch("shutil.which")
    def test_check_requirements_nssm_windows(self, mock_which, mock_system, sample_config):
        """Test NSSM check on Windows."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        mock_system.return_value = "Windows"
        mock_which.return_value = "C:\\tools\\nssm.exe"

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(sample_config, config_dir, state)

            results = installer.check_requirements()
            nssm_result = next((r for r in results if r["name"] == "nssm"), None)

            assert nssm_result is not None
            assert nssm_result["installed"] is True

    @patch("platform.system")
    def test_check_requirements_nssm_non_windows(self, mock_system, sample_config):
        """Test NSSM check on non-Windows."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        mock_system.return_value = "Linux"

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(sample_config, config_dir, state)

            results = installer.check_requirements()
            nssm_result = next((r for r in results if r["name"] == "nssm"), None)

            assert nssm_result is not None
            assert nssm_result["installed"] is True  # Skipped on non-Windows

    @patch("shutil.which")
    def test_check_requirements_claude_code(self, mock_which, sample_config):
        """Test Claude Code check."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        mock_which.return_value = "/usr/bin/claude"

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(sample_config, config_dir, state)

            results = installer.check_requirements()
            cc_result = next((r for r in results if r["name"] == "claude-code"), None)

            assert cc_result is not None
            assert cc_result["installed"] is True

    def test_create_static_configs_with_root(self, sample_config):
        """Test static config with {root} substitution."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(sample_config, config_dir, state)

            installer.create_static_configs()

            config_file = config_dir / "config.json"
            assert config_file.exists()
            content = json.loads(config_file.read_text())
            assert content["root"] == str(config_dir)

    @patch("subprocess.run")
    def test_clone_repos_already_exists(self, mock_run, sample_config):
        """Test cloning when repo directory already exists as git repo."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(sample_config, config_dir, state)

            # Create repo directory with .git
            repo_path = config_dir / ".projects" / "test"
            repo_path.mkdir(parents=True)
            (repo_path / ".git").mkdir()

            installer.clone_repos()

            # Should not call git clone
            mock_run.assert_not_called()
            assert "repos" in state.completed_steps

    @patch("subprocess.run")
    def test_clone_repos_directory_exists_not_git(self, mock_run, sample_config):
        """Test cloning when directory exists but is not a git repo."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(sample_config, config_dir, state)

            # Create repo directory without .git
            repo_path = config_dir / ".projects" / "test"
            repo_path.mkdir(parents=True)

            installer.clone_repos()

            # Should not complete repos step
            assert "repos" not in state.completed_steps
            assert any(s.step == "repos:test-repo" for s in state.failed_steps)

    def test_determine_pending_configs(self, sample_config):
        """Test determining pending configs."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        config_with_keys = HanielConfig(
            install=InstallConfig(
                configs={
                    "test-env": ConfigFileConfig(
                        path="./.env",
                        keys=[
                            ConfigKeyConfig(key="REQUIRED_KEY", prompt="Required"),
                            ConfigKeyConfig(key="DEFAULT_KEY", default="default"),
                        ],
                    ),
                },
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(config_with_keys, config_dir, state)

            installer.determine_pending_configs()

            # REQUIRED_KEY should be pending, DEFAULT_KEY should be auto-filled
            assert "test-env" in state.pending_configs
            assert "REQUIRED_KEY" in state.pending_configs["test-env"]
            assert "DEFAULT_KEY" in state.config_values.get("test-env", {})

    def test_load_existing_config_env(self, sample_config):
        """Test loading existing .env file."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(sample_config, config_dir, state)

            # Create .env file - name it with .env extension
            env_file = config_dir / "test.env"
            env_file.write_text("KEY1=value1\nKEY2=value2\n# comment\n")

            result = installer._load_existing_config(env_file, [])
            assert result["KEY1"] == "value1"
            assert result["KEY2"] == "value2"

    def test_load_existing_config_json(self, sample_config):
        """Test loading existing JSON config file."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(sample_config, config_dir, state)

            # Create JSON file
            json_file = config_dir / "config.json"
            json_file.write_text('{"key1": "value1", "key2": 123}')

            keys = [MagicMock(key="key1"), MagicMock(key="key2")]
            result = installer._load_existing_config(json_file, keys)
            assert result["key1"] == "value1"
            assert result["key2"] == "123"


class TestInteractiveInstallerExtended:
    """Extended tests for InteractiveInstaller."""

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
                                guide="Get from slack.com/apps",
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

    def test_has_pending_configs_no_install(self):
        """Test has_pending_configs with no install config."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState

        config = HanielConfig()  # No install config

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = InteractiveInstaller(config, config_dir, state)

            assert installer.has_pending_configs() is False

    def test_has_pending_configs_all_defaults(self):
        """Test has_pending_configs when all have defaults."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState

        config = HanielConfig(
            install=InstallConfig(
                configs={
                    "env": ConfigFileConfig(
                        path="./.env",
                        keys=[
                            ConfigKeyConfig(key="KEY1", default="default1"),
                            ConfigKeyConfig(key="KEY2", default="default2"),
                        ],
                    ),
                },
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = InteractiveInstaller(config, config_dir, state)

            assert installer.has_pending_configs() is False

    def test_set_config_no_configs(self, interactive_config):
        """Test set_config with no configs defined."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState

        config = HanielConfig()  # No install config

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = InteractiveInstaller(config, config_dir, state)

            result = installer.set_config("test", "key", "value")
            assert result["success"] is False
            assert "No configs defined" in result["error"]

    def test_set_config_unknown_config(self, interactive_config):
        """Test set_config with unknown config name."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = InteractiveInstaller(interactive_config, config_dir, state)

            result = installer.set_config("unknown-config", "key", "value")
            assert result["success"] is False
            assert "Unknown config" in result["error"]

    def test_set_config_unknown_key(self, interactive_config):
        """Test set_config with unknown key."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = InteractiveInstaller(interactive_config, config_dir, state)

            result = installer.set_config("workspace-env", "unknown-key", "value")
            assert result["success"] is False
            assert "Unknown key" in result["error"]

    def test_get_config_no_configs(self, interactive_config):
        """Test get_config with no configs defined."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState

        config = HanielConfig()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = InteractiveInstaller(config, config_dir, state)

            result = installer.get_config("test")
            assert "error" in result

    def test_get_config_no_keys(self, interactive_config):
        """Test get_config with config that has no keys."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState

        config = HanielConfig(
            install=InstallConfig(
                configs={
                    "static": ConfigFileConfig(
                        path="./config.json",
                        content='{"key": "value"}',
                        # No keys defined
                    ),
                },
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = InteractiveInstaller(config, config_dir, state)

            result = installer.get_config("static")
            assert "error" in result

    def test_finalize_install_missing_keys(self, interactive_config):
        """Test finalize_install with missing required keys."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState, InstallPhase

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.INTERACTIVE)
            installer = InteractiveInstaller(interactive_config, config_dir, state)

            result = installer.finalize_install()
            assert result["success"] is False
            assert "Missing keys" in result["error"]

    def test_get_mcp_tools(self, interactive_config):
        """Test get_mcp_tools returns all tools."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = InteractiveInstaller(interactive_config, config_dir, state)

            tools = installer.get_mcp_tools()
            tool_names = [t["name"] for t in tools]

            assert "haniel_install_status" in tool_names
            assert "haniel_set_config" in tool_names
            assert "haniel_get_config" in tool_names
            assert "haniel_retry_step" in tool_names
            assert "haniel_finalize_install" in tool_names

    @pytest.mark.asyncio
    async def test_call_mcp_tool_unknown(self, interactive_config):
        """Test call_mcp_tool with unknown tool."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = InteractiveInstaller(interactive_config, config_dir, state)

            result = await installer.call_mcp_tool("unknown_tool", {})
            result_data = json.loads(result)
            assert "error" in result_data


class TestFinalizerExtended:
    """Extended tests for Finalizer."""

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

    @patch("platform.system")
    @patch("shutil.which")
    @patch("subprocess.run")
    def test_register_nssm_service_set_commands(
        self, mock_run, mock_which, mock_system, finalizer_config
    ):
        """Test NSSM service registration with set commands."""
        from haniel.installer.finalize import Finalizer
        from haniel.installer.state import InstallState

        mock_system.return_value = "Windows"
        mock_which.side_effect = lambda cmd: {
            "nssm": r"C:\tools\nssm.exe",
            "python": r"C:\Python312\python.exe",
        }.get(cmd)
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            finalizer = Finalizer(finalizer_config, config_dir, state)

            finalizer.register_service()

            # Check that nssm commands were called
            calls = mock_run.call_args_list
            assert len(calls) >= 4  # remove, install, set DisplayName, set AppDirectory, etc

    def test_generate_config_files_creates_parent(self, finalizer_config):
        """Test that generate_config_files creates parent directories."""
        from haniel.installer.finalize import Finalizer
        from haniel.installer.state import InstallState, InstallPhase

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.FINALIZE)
            state.config_values["workspace-env"] = {
                "SLACK_TOKEN": "token",
                "DEBUG": "true",
            }
            finalizer = Finalizer(finalizer_config, config_dir, state)

            # workspace dir doesn't exist yet
            assert not (config_dir / "workspace").exists()

            finalizer.generate_config_files()

            # Should create workspace dir and .env file
            assert (config_dir / "workspace").exists()
            assert (config_dir / "workspace" / ".env").exists()


class TestOrchestratorPhases:
    """Tests for InstallOrchestrator phases."""

    @pytest.fixture
    def orchestrator_config(self):
        """Create a config for orchestration testing."""
        return HanielConfig(
            install=InstallConfig(
                requirements={"python": ">=3.11"},
                directories=["./runtime"],
                configs={
                    "test-env": ConfigFileConfig(
                        path="./.env",
                        keys=[
                            ConfigKeyConfig(key="TEST_KEY", prompt="Test Key"),
                        ],
                    ),
                },
            ),
        )

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_run_mechanical_phase(self, mock_run, mock_which, orchestrator_config):
        """Test running mechanical phase."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState, InstallPhase

        mock_which.return_value = "/usr/bin/python"
        mock_run.return_value = MagicMock(returncode=0, stdout="Python 3.12.0")

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.MECHANICAL)
            orchestrator = InstallOrchestrator(
                orchestrator_config, config_dir, state
            )

            result = orchestrator.run_mechanical_phase()

            assert result is True
            assert state.phase == InstallPhase.INTERACTIVE

    @patch("shutil.which")
    def test_run_interactive_phase_no_pending(self, mock_which, orchestrator_config):
        """Test interactive phase with no pending configs."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState, InstallPhase

        # Config without keys
        config_no_keys = HanielConfig(
            install=InstallConfig(
                configs={
                    "static": ConfigFileConfig(
                        path="./config.json",
                        content='{"key": "value"}',
                    ),
                },
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.INTERACTIVE)
            orchestrator = InstallOrchestrator(
                config_no_keys, config_dir, state
            )

            result = orchestrator.run_interactive_phase()

            assert result is True
            assert state.phase == InstallPhase.FINALIZE

    def test_run_finalize_phase_missing_configs(self, orchestrator_config):
        """Test finalize phase with missing configs."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState, InstallPhase

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.FINALIZE)
            # Don't set required config values
            orchestrator = InstallOrchestrator(
                orchestrator_config, config_dir, state
            )

            result = orchestrator.run_finalize_phase()

            assert result is False

    def test_run_finalize_phase_success(self, orchestrator_config):
        """Test successful finalize phase."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState, InstallPhase

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.FINALIZE)
            state.config_values["test-env"] = {"TEST_KEY": "value"}
            orchestrator = InstallOrchestrator(
                orchestrator_config, config_dir, state
            )

            result = orchestrator.run_finalize_phase()

            assert result is True
            assert state.phase == InstallPhase.COMPLETE

    def test_retry_repos_step(self, orchestrator_config):
        """Test retrying repos step."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState, InstallPhase

        config_with_repo = HanielConfig(
            install=InstallConfig(),
            repos={
                "test-repo": RepoConfig(
                    url="https://github.com/test/test.git",
                    branch="main",
                    path="./.projects/test",
                ),
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.MECHANICAL)

            # Create repo directory with .git
            repo_path = config_dir / ".projects" / "test"
            repo_path.mkdir(parents=True)
            (repo_path / ".git").mkdir()

            orchestrator = InstallOrchestrator(
                config_with_repo, config_dir, state
            )

            result = orchestrator.retry_step("repos")

            assert result["success"] is True

    @patch("subprocess.run")
    def test_retry_requirements_step(self, mock_run, orchestrator_config):
        """Test retrying requirements step."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState, InstallPhase

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Python 3.12.0",
            stderr=""
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.MECHANICAL)
            orchestrator = InstallOrchestrator(
                orchestrator_config, config_dir, state
            )

            result = orchestrator.retry_step("requirements:python")

            assert result["success"] is True


class TestInstallStateExtended:
    """Extended tests for InstallState."""

    def test_state_transitions(self):
        """Test state transitions."""
        from haniel.installer.state import InstallState, InstallPhase

        state = InstallState()
        assert state.phase == InstallPhase.NOT_STARTED

        state.start_installation()
        # start_installation transitions to BOOTSTRAP

        state.transition_to(InstallPhase.MECHANICAL)
        assert state.phase == InstallPhase.MECHANICAL

        state.transition_to(InstallPhase.INTERACTIVE)
        assert state.phase == InstallPhase.INTERACTIVE

    def test_is_step_complete(self):
        """Test checking if step is complete."""
        from haniel.installer.state import InstallState

        state = InstallState()
        assert state.is_step_complete("test") is False

        state.mark_complete("test")
        assert state.is_step_complete("test") is True

    def test_is_complete(self):
        """Test checking if installation is complete."""
        from haniel.installer.state import InstallState, InstallPhase

        state = InstallState()
        assert state.is_complete() is False

        state.transition_to(InstallPhase.COMPLETE)
        assert state.is_complete() is True

    def test_set_config_value(self):
        """Test setting config value."""
        from haniel.installer.state import InstallState

        state = InstallState()
        state.set_config_value("config1", "key1", "value1")

        assert state.config_values["config1"]["key1"] == "value1"

    def test_clear_failure(self):
        """Test clearing a failure."""
        from haniel.installer.state import InstallState

        state = InstallState()
        state.mark_failed("test-step", "error message")
        assert len(state.failed_steps) == 1

        state.clear_failure("test-step")
        assert len(state.failed_steps) == 0


class TestMechanicalInstallerEnvironments:
    """Tests for MechanicalInstaller environment creation."""

    @pytest.fixture
    def env_config(self):
        """Create a config with environments."""
        return HanielConfig(
            install=InstallConfig(
                environments={
                    "main-venv": EnvironmentConfig(
                        type="python-venv",
                        path="./runtime/venv",
                        requirements=["./requirements.txt"],
                    ),
                    "npm-project": EnvironmentConfig(
                        type="npm",
                        path="./runtime/npm",
                    ),
                    "unknown-type": EnvironmentConfig(
                        type="unknown",
                        path="./runtime/unknown",
                    ),
                },
            ),
        )

    @patch("subprocess.run")
    def test_create_python_venv_success(self, mock_run, env_config):
        """Test creating Python venv successfully."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        mock_run.return_value = MagicMock(returncode=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(env_config, config_dir, state)

            # Create requirements file
            (config_dir / "requirements.txt").write_text("pytest\n")

            result = installer._create_python_venv(
                "main-venv",
                config_dir / "runtime" / "venv",
                ["./requirements.txt"],
            )

            assert result is True

    @patch("subprocess.run")
    def test_create_python_venv_failure(self, mock_run, env_config):
        """Test Python venv creation failure."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState
        import subprocess

        mock_run.side_effect = subprocess.CalledProcessError(1, "python")

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(env_config, config_dir, state)

            result = installer._create_python_venv(
                "main-venv",
                config_dir / "runtime" / "venv",
                None,
            )

            assert result is False

    @patch("subprocess.run")
    def test_npm_install_success(self, mock_run, env_config):
        """Test npm install success."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        mock_run.return_value = MagicMock(returncode=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(env_config, config_dir, state)

            # Create npm project
            npm_path = config_dir / "runtime" / "npm"
            npm_path.mkdir(parents=True)
            (npm_path / "package.json").write_text('{"name": "test"}')

            result = installer._run_npm_install("npm-project", npm_path)

            assert result is True

    def test_npm_install_no_package_json(self, env_config):
        """Test npm install with no package.json."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(env_config, config_dir, state)

            # Create empty directory
            npm_path = config_dir / "runtime" / "npm"
            npm_path.mkdir(parents=True)

            result = installer._run_npm_install("npm-project", npm_path)

            # Returns True - not an error
            assert result is True

    @patch("subprocess.run")
    def test_npm_install_failure(self, mock_run, env_config):
        """Test npm install failure."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState
        import subprocess

        mock_run.side_effect = subprocess.CalledProcessError(1, "npm")

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(env_config, config_dir, state)

            npm_path = config_dir / "runtime" / "npm"
            npm_path.mkdir(parents=True)
            (npm_path / "package.json").write_text('{"name": "test"}')

            result = installer._run_npm_install("npm-project", npm_path)

            assert result is False

    def test_create_environments_unknown_type(self, env_config):
        """Test creating environment with unknown type."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        # Only include the unknown type
        config_unknown = HanielConfig(
            install=InstallConfig(
                environments={
                    "unknown": EnvironmentConfig(
                        type="unknown_type",
                        path="./runtime/unknown",
                    ),
                },
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(config_unknown, config_dir, state)

            installer.create_environments()

            # Should not complete environments step
            assert "environments" not in state.completed_steps


class TestOrchestratorRetryRequirements:
    """Tests for orchestrator retry requirements."""

    @pytest.fixture
    def config_with_requirements(self):
        """Create a config with requirements."""
        return HanielConfig(
            install=InstallConfig(
                requirements={"python": ">=3.11", "node": ">=18"},
            ),
        )

    @patch("subprocess.run")
    def test_retry_requirement_not_found(self, mock_run, config_with_requirements):
        """Test retrying unknown requirement."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState

        mock_run.return_value = MagicMock(returncode=0, stdout="Python 3.12.0")

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            orchestrator = InstallOrchestrator(
                config_with_requirements, config_dir, state
            )

            result = orchestrator.retry_step("requirements:unknown")

            assert result["success"] is False
            assert "Unknown requirement" in result["error"]

    @patch("subprocess.run")
    def test_retry_requirement_fails(self, mock_run, config_with_requirements):
        """Test retrying requirement that still fails."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState

        mock_run.side_effect = Exception("Python not found")

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            orchestrator = InstallOrchestrator(
                config_with_requirements, config_dir, state
            )

            result = orchestrator.retry_step("requirements:python")

            assert result["success"] is False


class TestOrchestratorFullInstall:
    """Tests for orchestrator full install flow."""

    @pytest.fixture
    def simple_config(self):
        """Create a simple config for testing."""
        return HanielConfig(
            install=InstallConfig(
                directories=["./runtime"],
            ),
        )

    @patch("shutil.which")
    @patch("haniel.installer.orchestrator.InstallOrchestrator.run_bootstrap_phase")
    @patch("haniel.installer.orchestrator.InstallOrchestrator.run_mechanical_phase")
    @patch("haniel.installer.orchestrator.InstallOrchestrator.run_interactive_phase")
    @patch("haniel.installer.orchestrator.InstallOrchestrator.run_finalize_phase")
    def test_run_full_install_fresh(
        self,
        mock_finalize,
        mock_interactive,
        mock_mechanical,
        mock_bootstrap,
        mock_which,
        simple_config,
    ):
        """Test running full install from scratch."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState, InstallPhase

        mock_which.return_value = "/usr/bin/claude"

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            orchestrator = InstallOrchestrator(simple_config, config_dir, state)

            # Each phase transitions to the next
            def bootstrap_effect():
                orchestrator.state.transition_to(InstallPhase.MECHANICAL)
                return True

            def mechanical_effect():
                orchestrator.state.transition_to(InstallPhase.INTERACTIVE)
                return True

            def interactive_effect(on_status=None):
                orchestrator.state.transition_to(InstallPhase.FINALIZE)
                return True

            def finalize_effect():
                orchestrator.state.transition_to(InstallPhase.COMPLETE)
                return True

            mock_bootstrap.side_effect = bootstrap_effect
            mock_mechanical.side_effect = mechanical_effect
            mock_interactive.side_effect = interactive_effect
            mock_finalize.side_effect = finalize_effect

            result = orchestrator.run_full_install()

            assert result is True

    @patch("shutil.which")
    def test_run_full_install_resume(self, mock_which, simple_config):
        """Test resuming install from saved state."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState, InstallPhase

        mock_which.return_value = "/usr/bin/claude"

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)

            # Save state at COMPLETE phase
            state = InstallState(phase=InstallPhase.COMPLETE)
            state.save(config_dir / "install.state")

            # Start fresh but resume
            new_state = InstallState()
            orchestrator = InstallOrchestrator(simple_config, config_dir, new_state)

            result = orchestrator.run_full_install(resume=True)

            # State should have been loaded and is already complete
            assert result is True

    @patch("shutil.which")
    def test_run_full_install_bootstrap_fails(self, mock_which, simple_config):
        """Test full install when bootstrap fails."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState

        mock_which.return_value = None  # Claude not found

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            orchestrator = InstallOrchestrator(simple_config, config_dir, state)

            result = orchestrator.run_full_install()

            assert result is False


class TestInteractiveInstallerSession:
    """Tests for InteractiveInstaller session handling."""

    @pytest.fixture
    def session_config(self):
        """Create a config for session testing."""
        return HanielConfig(
            install=InstallConfig(
                configs={
                    "test-env": ConfigFileConfig(
                        path="./test/.env",
                        keys=[
                            ConfigKeyConfig(
                                key="SECRET",
                                prompt="Secret Value",
                                guide="Get from settings page",
                            ),
                        ],
                    ),
                },
            ),
        )

    def test_retry_step_delegates_to_orchestrator(self, session_config):
        """Test that retry_step delegates to orchestrator."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState, InstallPhase

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.MECHANICAL)

            # Config with directories for retry
            simple_config = HanielConfig(
                install=InstallConfig(
                    directories=["./runtime"],
                ),
            )
            installer = InteractiveInstaller(simple_config, config_dir, state)

            result = installer.retry_step("directories")

            # Should succeed
            assert result["success"] is True

    @pytest.mark.asyncio
    async def test_call_mcp_tool_retry_step(self, session_config):
        """Test calling retry_step through MCP."""
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState, InstallPhase

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.MECHANICAL)
            installer = InteractiveInstaller(session_config, config_dir, state)

            # Retry an unknown step
            result = await installer.call_mcp_tool("haniel_retry_step", {"step_name": "unknown"})
            result_data = json.loads(result)

            assert result_data["success"] is False


class TestMechanicalInstallerCloneRepos:
    """Tests for MechanicalInstaller clone repos."""

    @pytest.fixture
    def repo_config(self):
        """Create a config with repos."""
        return HanielConfig(
            repos={
                "test-repo": RepoConfig(
                    url="https://github.com/test/test.git",
                    branch="main",
                    path="./.projects/test",
                ),
            },
        )

    @patch("subprocess.run")
    def test_clone_repos_timeout(self, mock_run, repo_config):
        """Test clone repos timeout handling."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired("git", 300)

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(repo_config, config_dir, state)

            installer.clone_repos()

            # Should not complete repos step
            assert "repos" not in state.completed_steps
            assert any(s.step == "repos:test-repo" for s in state.failed_steps)

    @patch("subprocess.run")
    def test_clone_repos_error(self, mock_run, repo_config):
        """Test clone repos error handling."""
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        mock_run.return_value = MagicMock(returncode=1, stderr="Permission denied")

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            installer = MechanicalInstaller(repo_config, config_dir, state)

            installer.clone_repos()

            # Should not complete repos step
            assert "repos" not in state.completed_steps


class TestFinalizerConfigGeneration:
    """Tests for Finalizer config generation."""

    @pytest.fixture
    def json_config(self):
        """Create a config with JSON output."""
        return HanielConfig(
            install=InstallConfig(
                configs={
                    "json-config": ConfigFileConfig(
                        path="./config.json",
                        keys=[
                            ConfigKeyConfig(key="API_KEY", prompt="API Key"),
                            ConfigKeyConfig(key="DEBUG", default="false"),
                        ],
                    ),
                },
            ),
        )

    def test_generate_json_config(self, json_config):
        """Test generating JSON config file."""
        from haniel.installer.finalize import Finalizer
        from haniel.installer.state import InstallState, InstallPhase

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.FINALIZE)
            state.config_values["json-config"] = {
                "API_KEY": "secret-key",
                "DEBUG": "true",
            }
            finalizer = Finalizer(json_config, config_dir, state)

            finalizer.generate_config_files()

            config_file = config_dir / "config.json"
            assert config_file.exists()

            content = json.loads(config_file.read_text())
            assert content["API_KEY"] == "secret-key"
            assert content["DEBUG"] == "true"

    def test_check_all_configs_filled_no_configs(self):
        """Test check_all_configs_filled with no configs."""
        from haniel.installer.finalize import Finalizer
        from haniel.installer.state import InstallState

        config = HanielConfig()  # No install config

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            finalizer = Finalizer(config, config_dir, state)

            # Should return True - nothing to check
            assert finalizer.check_all_configs_filled() is True


class TestOrchestratorProperties:
    """Tests for orchestrator property accessors."""

    @pytest.fixture
    def simple_config(self):
        """Create a simple config."""
        return HanielConfig(
            install=InstallConfig(
                directories=["./runtime"],
            ),
        )

    def test_mechanical_property(self, simple_config):
        """Test mechanical property lazy initialization."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.mechanical import MechanicalInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            orchestrator = InstallOrchestrator(simple_config, config_dir, state)

            mechanical = orchestrator.mechanical
            assert isinstance(mechanical, MechanicalInstaller)

            # Should return same instance
            assert orchestrator.mechanical is mechanical

    def test_interactive_property(self, simple_config):
        """Test interactive property lazy initialization."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.interactive import InteractiveInstaller
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            orchestrator = InstallOrchestrator(simple_config, config_dir, state)

            interactive = orchestrator.interactive
            assert isinstance(interactive, InteractiveInstaller)

            # Should return same instance
            assert orchestrator.interactive is interactive

    def test_finalizer_property(self, simple_config):
        """Test finalizer property lazy initialization."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.finalize import Finalizer
        from haniel.installer.state import InstallState

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState()
            orchestrator = InstallOrchestrator(simple_config, config_dir, state)

            finalizer = orchestrator.finalizer
            assert isinstance(finalizer, Finalizer)

            # Should return same instance
            assert orchestrator.finalizer is finalizer


class TestOrchestratorMechanicalPhaseErrors:
    """Tests for mechanical phase error handling."""

    @pytest.fixture
    def full_config(self):
        """Create a config with all mechanical steps."""
        return HanielConfig(
            install=InstallConfig(
                requirements={"python": ">=3.11"},
                directories=["./runtime"],
                environments={
                    "test-venv": EnvironmentConfig(
                        type="python-venv",
                        path="./runtime/venv",
                    ),
                },
                configs={
                    "static": ConfigFileConfig(
                        path="./config.json",
                        content='{"test": true}',
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

    @patch("haniel.installer.mechanical.MechanicalInstaller.create_directories")
    @patch("subprocess.run")
    def test_mechanical_phase_directories_error(self, mock_run, mock_dirs, full_config):
        """Test mechanical phase when directories fail."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState, InstallPhase

        mock_run.return_value = MagicMock(returncode=0, stdout="Python 3.12.0")
        mock_dirs.side_effect = Exception("Permission denied")

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.MECHANICAL)
            orchestrator = InstallOrchestrator(full_config, config_dir, state)

            # Should still complete but record failure
            result = orchestrator.run_mechanical_phase()

            # Phase continues despite error
            assert result is True
            assert any(s.step == "directories" for s in state.failed_steps)

    @patch("haniel.installer.mechanical.MechanicalInstaller.clone_repos")
    @patch("haniel.installer.mechanical.MechanicalInstaller.create_directories")
    @patch("subprocess.run")
    def test_mechanical_phase_repos_error(self, mock_run, mock_dirs, mock_repos, full_config):
        """Test mechanical phase when repos fail."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState, InstallPhase

        mock_run.return_value = MagicMock(returncode=0, stdout="Python 3.12.0")
        mock_repos.side_effect = Exception("Clone failed")

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.MECHANICAL)
            orchestrator = InstallOrchestrator(full_config, config_dir, state)

            result = orchestrator.run_mechanical_phase()

            assert result is True
            assert any(s.step == "repos" for s in state.failed_steps)

    @patch("haniel.installer.mechanical.MechanicalInstaller.create_environments")
    @patch("haniel.installer.mechanical.MechanicalInstaller.clone_repos")
    @patch("haniel.installer.mechanical.MechanicalInstaller.create_directories")
    @patch("subprocess.run")
    def test_mechanical_phase_environments_error(
        self, mock_run, mock_dirs, mock_repos, mock_envs, full_config
    ):
        """Test mechanical phase when environments fail."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState, InstallPhase

        mock_run.return_value = MagicMock(returncode=0, stdout="Python 3.12.0")
        mock_envs.side_effect = Exception("Venv creation failed")

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.MECHANICAL)
            orchestrator = InstallOrchestrator(full_config, config_dir, state)

            result = orchestrator.run_mechanical_phase()

            assert result is True
            assert any(s.step == "environments" for s in state.failed_steps)

    @patch("haniel.installer.mechanical.MechanicalInstaller.create_static_configs")
    @patch("haniel.installer.mechanical.MechanicalInstaller.create_environments")
    @patch("haniel.installer.mechanical.MechanicalInstaller.clone_repos")
    @patch("haniel.installer.mechanical.MechanicalInstaller.create_directories")
    @patch("subprocess.run")
    def test_mechanical_phase_static_configs_error(
        self, mock_run, mock_dirs, mock_repos, mock_envs, mock_configs, full_config
    ):
        """Test mechanical phase when static configs fail."""
        from haniel.installer.orchestrator import InstallOrchestrator
        from haniel.installer.state import InstallState, InstallPhase

        mock_run.return_value = MagicMock(returncode=0, stdout="Python 3.12.0")
        mock_configs.side_effect = Exception("Write failed")

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            state = InstallState(phase=InstallPhase.MECHANICAL)
            orchestrator = InstallOrchestrator(full_config, config_dir, state)

            result = orchestrator.run_mechanical_phase()

            assert result is True
            assert any(s.step == "static-configs" for s in state.failed_steps)


class TestInstallMcpServerUnit:
    """Unit tests for InstallMcpServer."""

    def test_init(self):
        """Test InstallMcpServer initialization."""
        from haniel.installer.install_mcp_server import InstallMcpServer, DEFAULT_INSTALL_MCP_PORT

        mock_installer = MagicMock()
        server = InstallMcpServer(mock_installer)

        assert server.installer is mock_installer
        assert server.port == DEFAULT_INSTALL_MCP_PORT
        assert server._app_runner is None
        assert server._stop_event is None
        assert server._server_thread is None
        assert server._loop is None

    def test_init_custom_port(self):
        """Test InstallMcpServer initialization with custom port."""
        from haniel.installer.install_mcp_server import InstallMcpServer

        mock_installer = MagicMock()
        server = InstallMcpServer(mock_installer, port=4000)

        assert server.port == 4000

    def test_list_tools(self):
        """Test list_tools delegates to installer."""
        from haniel.installer.install_mcp_server import InstallMcpServer

        mock_installer = MagicMock()
        mock_installer.get_mcp_tools.return_value = [
            {"name": "tool1", "description": "Test tool"}
        ]

        server = InstallMcpServer(mock_installer)
        tools = server.list_tools()

        assert tools == [{"name": "tool1", "description": "Test tool"}]
        mock_installer.get_mcp_tools.assert_called_once()

    @pytest.mark.asyncio
    async def test_call_tool(self):
        """Test call_tool delegates to installer."""
        from haniel.installer.install_mcp_server import InstallMcpServer

        mock_installer = MagicMock()
        mock_installer.call_mcp_tool = AsyncMock(return_value="result")

        server = InstallMcpServer(mock_installer)
        result = await server.call_tool("test_tool", {"arg": "value"})

        assert result == "result"
        mock_installer.call_mcp_tool.assert_called_once_with("test_tool", {"arg": "value"})

    def test_is_running_false_no_thread(self):
        """Test is_running returns False when no thread exists."""
        from haniel.installer.install_mcp_server import InstallMcpServer

        mock_installer = MagicMock()
        server = InstallMcpServer(mock_installer)

        assert server.is_running() is False

    def test_is_running_false_thread_dead(self):
        """Test is_running returns False when thread is not alive."""
        from haniel.installer.install_mcp_server import InstallMcpServer

        mock_installer = MagicMock()
        server = InstallMcpServer(mock_installer)

        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = False
        server._server_thread = mock_thread

        assert server.is_running() is False

    def test_is_running_true(self):
        """Test is_running returns True when thread is alive."""
        from haniel.installer.install_mcp_server import InstallMcpServer

        mock_installer = MagicMock()
        server = InstallMcpServer(mock_installer)

        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        server._server_thread = mock_thread

        assert server.is_running() is True

    @pytest.mark.asyncio
    async def test_stop_no_event(self):
        """Test stop when no stop event exists."""
        from haniel.installer.install_mcp_server import InstallMcpServer

        mock_installer = MagicMock()
        server = InstallMcpServer(mock_installer)

        # Should not raise
        await server.stop()
        assert server._app_runner is None

    @pytest.mark.asyncio
    async def test_stop_with_runner_cleanup_error(self):
        """Test stop handles runner cleanup error gracefully."""
        from haniel.installer.install_mcp_server import InstallMcpServer

        mock_installer = MagicMock()
        server = InstallMcpServer(mock_installer)

        mock_runner = MagicMock()
        mock_runner.cleanup = AsyncMock(side_effect=Exception("Cleanup error"))
        server._app_runner = mock_runner
        server._stop_event = MagicMock()

        # Should not raise
        await server.stop()
        assert server._app_runner is None

    def test_stop_background_no_event(self):
        """Test stop_background when no stop event exists."""
        from haniel.installer.install_mcp_server import InstallMcpServer

        mock_installer = MagicMock()
        server = InstallMcpServer(mock_installer)

        # Should not raise
        server.stop_background()

    def test_stop_background_thread_alive(self):
        """Test stop_background with alive thread."""
        from haniel.installer.install_mcp_server import InstallMcpServer

        mock_installer = MagicMock()
        server = InstallMcpServer(mock_installer)

        # Set up mocks - thread starts alive then becomes dead after join
        mock_loop = MagicMock()
        mock_event = MagicMock()
        mock_thread = MagicMock()
        # First call (before join check) returns True, second call (after join) returns False
        mock_thread.is_alive.side_effect = [True, False]

        server._loop = mock_loop
        server._stop_event = mock_event
        server._server_thread = mock_thread

        server.stop_background()

        mock_loop.call_soon_threadsafe.assert_called_once_with(mock_event.set)
        mock_thread.join.assert_called_once_with(timeout=5.0)

    def test_stop_background_thread_timeout(self):
        """Test stop_background when thread doesn't stop cleanly."""
        from haniel.installer.install_mcp_server import InstallMcpServer

        mock_installer = MagicMock()
        server = InstallMcpServer(mock_installer)

        # Set up mocks - thread stays alive after join
        mock_loop = MagicMock()
        mock_event = MagicMock()
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True  # Thread doesn't stop

        server._loop = mock_loop
        server._stop_event = mock_event
        server._server_thread = mock_thread

        # Should log warning but not raise
        server.stop_background()

        mock_thread.join.assert_called_once_with(timeout=5.0)

    @pytest.mark.asyncio
    async def test_start_import_error(self):
        """Test start handles import error."""
        from haniel.installer.install_mcp_server import InstallMcpServer

        mock_installer = MagicMock()
        server = InstallMcpServer(mock_installer)

        with patch.dict("sys.modules", {"mcp.server": None}):
            with patch("haniel.installer.install_mcp_server.asyncio.Event") as mock_event_class:
                mock_event = MagicMock()
                mock_event_class.return_value = mock_event

                # Import will fail when trying to use the module
                with pytest.raises(Exception):
                    await server.start()
