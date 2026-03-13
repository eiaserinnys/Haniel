"""Tests for haniel configuration parsing."""

from pathlib import Path

import pytest

from haniel.config import (
    HanielConfig,
    RepoConfig,
    ServiceConfig,
    ShutdownConfig,
    BackoffConfig,
    load_config,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TestHanielConfigParsing:
    """Test configuration file parsing."""

    def test_load_valid_config(self):
        """Should successfully load a valid configuration file."""
        config = load_config(FIXTURES_DIR / "valid_config.yaml")
        assert isinstance(config, HanielConfig)
        assert config.poll_interval == 60

    def test_poll_interval_default(self):
        """poll_interval should have a sensible default."""
        config = HanielConfig(repos={}, services={})
        assert config.poll_interval == 60

    def test_repos_parsing(self):
        """Should parse repos section correctly."""
        config = load_config(FIXTURES_DIR / "valid_config.yaml")
        assert "test-repo" in config.repos
        repo = config.repos["test-repo"]
        assert isinstance(repo, RepoConfig)
        assert repo.url == "git@github.com:example/test.git"
        assert repo.branch == "main"
        assert repo.path == "./projects/test"

    def test_services_parsing(self):
        """Should parse services section correctly."""
        config = load_config(FIXTURES_DIR / "valid_config.yaml")
        assert "mcp-server" in config.services
        service = config.services["mcp-server"]
        assert isinstance(service, ServiceConfig)
        assert service.run == "python -m mcp_server --port 3100"
        assert service.cwd == "./projects/test"
        assert service.repo == "test-repo"
        assert service.ready == "port:3100"

    def test_service_after_field(self):
        """Should parse after field (single dependency)."""
        config = load_config(FIXTURES_DIR / "valid_config.yaml")
        service = config.services["main-app"]
        assert service.after == ["mcp-server"]

    def test_service_shutdown_config(self):
        """Should parse service shutdown configuration."""
        config = load_config(FIXTURES_DIR / "valid_config.yaml")
        service = config.services["main-app"]
        assert service.shutdown is not None
        assert service.shutdown.signal == "SIGTERM"
        assert service.shutdown.timeout == 15

    def test_global_shutdown_config(self):
        """Should parse global shutdown configuration."""
        config = load_config(FIXTURES_DIR / "valid_config.yaml")
        assert config.shutdown is not None
        assert config.shutdown.timeout == 10
        assert config.shutdown.kill_timeout == 30

    def test_backoff_config(self):
        """Should parse backoff configuration."""
        config = load_config(FIXTURES_DIR / "valid_config.yaml")
        assert config.backoff is not None
        assert config.backoff.base_delay == 5
        assert config.backoff.max_delay == 300
        assert config.backoff.circuit_breaker == 5
        assert config.backoff.circuit_window == 300

    def test_webhooks_parsing(self):
        """Should parse webhooks configuration."""
        config = load_config(FIXTURES_DIR / "valid_config.yaml")
        assert config.webhooks is not None
        assert len(config.webhooks) == 1
        webhook = config.webhooks[0]
        assert webhook.url == "https://hooks.slack.com/services/T.../B.../..."
        assert webhook.format == "slack"

    def test_mcp_config(self):
        """Should parse MCP configuration."""
        config = load_config(FIXTURES_DIR / "valid_config.yaml")
        assert config.mcp is not None
        assert config.mcp.enabled is True
        assert config.mcp.transport == "sse"
        assert config.mcp.port == 3200

    def test_install_config_requirements(self):
        """Should parse install requirements."""
        config = load_config(FIXTURES_DIR / "valid_config.yaml")
        assert config.install is not None
        assert config.install.requirements is not None
        assert config.install.requirements.get("python") == ">=3.11"
        assert config.install.requirements.get("node") == ">=18"

    def test_install_config_directories(self):
        """Should parse install directories."""
        config = load_config(FIXTURES_DIR / "valid_config.yaml")
        assert config.install.directories is not None
        assert "./runtime" in config.install.directories
        assert "./runtime/logs" in config.install.directories

    def test_install_config_environments(self):
        """Should parse install environments."""
        config = load_config(FIXTURES_DIR / "valid_config.yaml")
        assert config.install.environments is not None
        assert "main-venv" in config.install.environments
        env = config.install.environments["main-venv"]
        assert env.type == "python-venv"
        assert env.path == "./runtime/venv"

    def test_install_config_service(self):
        """Should parse install service configuration."""
        config = load_config(FIXTURES_DIR / "valid_config.yaml")
        assert config.install.service is not None
        assert config.install.service.name == "haniel"
        assert config.install.service.display == "Haniel Service Runner"


class TestServiceConfigNormalization:
    """Test that service config values are normalized correctly."""

    def test_after_string_becomes_list(self):
        """Single after value should be normalized to a list."""
        config_data = {
            "poll_interval": 60,
            "repos": {},
            "services": {"svc": {"run": "echo hello", "after": "other-service"}},
        }
        config = HanielConfig.model_validate(config_data)
        assert config.services["svc"].after == ["other-service"]

    def test_after_list_stays_list(self):
        """After list should remain as list."""
        config_data = {
            "poll_interval": 60,
            "repos": {},
            "services": {"svc": {"run": "echo hello", "after": ["svc-a", "svc-b"]}},
        }
        config = HanielConfig.model_validate(config_data)
        assert config.services["svc"].after == ["svc-a", "svc-b"]

    def test_enabled_defaults_to_true(self):
        """Service enabled should default to True."""
        config_data = {
            "poll_interval": 60,
            "repos": {},
            "services": {"svc": {"run": "echo hello"}},
        }
        config = HanielConfig.model_validate(config_data)
        assert config.services["svc"].enabled is True


class TestConfigDefaults:
    """Test default values for optional fields."""

    def test_minimal_config(self):
        """Should accept minimal configuration."""
        config = HanielConfig(repos={}, services={})
        assert config.poll_interval == 60
        assert config.shutdown is None
        assert config.backoff is None
        assert config.webhooks is None
        assert config.mcp is None
        assert config.install is None

    def test_backoff_defaults(self):
        """Backoff config should have sensible defaults."""
        backoff = BackoffConfig()
        assert backoff.base_delay == 5
        assert backoff.max_delay == 300
        assert backoff.circuit_breaker == 5
        assert backoff.circuit_window == 300

    def test_shutdown_defaults(self):
        """Shutdown config should have sensible defaults."""
        shutdown = ShutdownConfig()
        assert shutdown.timeout == 10
        assert shutdown.kill_timeout == 30
        assert shutdown.signal == "SIGTERM"


class TestConfigFileNotFound:
    """Test error handling for missing config files."""

    def test_load_nonexistent_file(self):
        """Should raise FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            load_config(Path("/nonexistent/config.yaml"))


class TestInvalidYAML:
    """Test error handling for invalid YAML syntax."""

    def test_invalid_yaml_syntax(self, tmp_path: Path):
        """Should raise error for invalid YAML."""
        invalid_yaml = tmp_path / "invalid.yaml"
        invalid_yaml.write_text("invalid: yaml: syntax: [")

        with pytest.raises(Exception):  # yaml.YAMLError or similar
            load_config(invalid_yaml)
