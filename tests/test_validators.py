"""Tests for haniel configuration validators."""

from pathlib import Path

import pytest


from haniel.config import load_config
from haniel.config.validators import (
    validate_config,
    check_circular_dependencies,
    check_port_conflicts,
    check_duplicate_paths,
    check_missing_references,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TestValidateConfig:
    """Test the main validate_config function."""

    def test_valid_config_passes(self):
        """Valid config should pass all validations."""
        config = load_config(FIXTURES_DIR / "valid_config.yaml")
        errors = validate_config(config)
        assert len(errors) == 0

    def test_invalid_circular_fails(self):
        """Config with circular dependencies should fail."""
        config = load_config(FIXTURES_DIR / "invalid_circular.yaml")
        errors = validate_config(config)
        assert len(errors) > 0
        assert any("circular" in e.message.lower() for e in errors)

    def test_invalid_port_conflict_fails(self):
        """Config with port conflicts should fail."""
        config = load_config(FIXTURES_DIR / "invalid_port_conflict.yaml")
        errors = validate_config(config)
        assert len(errors) > 0
        assert any("port" in e.message.lower() for e in errors)

    def test_invalid_path_duplicate_fails(self):
        """Config with duplicate paths should fail."""
        config = load_config(FIXTURES_DIR / "invalid_path_duplicate.yaml")
        errors = validate_config(config)
        assert len(errors) > 0
        assert any(
            "path" in e.message.lower() or "duplicate" in e.message.lower()
            for e in errors
        )


class TestCircularDependencies:
    """Test circular dependency detection."""

    def test_no_dependencies_passes(self):
        """Services without dependencies should pass."""
        config = load_config(FIXTURES_DIR / "valid_config.yaml")
        errors = check_circular_dependencies(config)
        assert len(errors) == 0

    def test_simple_cycle_detected(self):
        """Simple A -> B -> A cycle should be detected."""
        config = load_config(FIXTURES_DIR / "invalid_circular.yaml")
        errors = check_circular_dependencies(config)
        assert len(errors) > 0
        # Should identify the cycle
        error_msg = errors[0].message.lower()
        assert "circular" in error_msg or "cycle" in error_msg

    def test_self_reference_detected(self):
        """Service depending on itself should be detected."""
        from haniel.config import HanielConfig, ServiceConfig

        config = HanielConfig(
            repos={},
            services={"self-ref": ServiceConfig(run="echo hello", after=["self-ref"])},
        )
        errors = check_circular_dependencies(config)
        assert len(errors) > 0


class TestPortConflicts:
    """Test port conflict detection."""

    def test_unique_ports_pass(self):
        """Services with unique ports should pass."""
        config = load_config(FIXTURES_DIR / "valid_config.yaml")
        errors = check_port_conflicts(config)
        assert len(errors) == 0

    def test_duplicate_ports_detected(self):
        """Services with duplicate ports should be detected."""
        config = load_config(FIXTURES_DIR / "invalid_port_conflict.yaml")
        errors = check_port_conflicts(config)
        assert len(errors) > 0
        # Should mention port 3100
        error_msg = errors[0].message
        assert "3100" in error_msg

    def test_no_ready_ports_passes(self):
        """Services without ready: port should pass."""
        from haniel.config import HanielConfig, ServiceConfig

        config = HanielConfig(
            repos={},
            services={
                "svc-a": ServiceConfig(run="echo a", ready="delay:5"),
                "svc-b": ServiceConfig(run="echo b", ready="log:started"),
            },
        )
        errors = check_port_conflicts(config)
        assert len(errors) == 0


class TestDuplicatePaths:
    """Test duplicate path detection."""

    def test_unique_paths_pass(self):
        """Repos with unique paths should pass."""
        config = load_config(FIXTURES_DIR / "valid_config.yaml")
        errors = check_duplicate_paths(config)
        assert len(errors) == 0

    def test_duplicate_paths_detected(self):
        """Repos with duplicate paths should be detected."""
        config = load_config(FIXTURES_DIR / "invalid_path_duplicate.yaml")
        errors = check_duplicate_paths(config)
        assert len(errors) > 0
        # Should mention the conflicting repos
        error_msg = errors[0].message
        assert "repo-a" in error_msg or "repo-b" in error_msg

    def test_empty_repos_passes(self):
        """Empty repos section should pass."""
        from haniel.config import HanielConfig

        config = HanielConfig(repos={}, services={})
        errors = check_duplicate_paths(config)
        assert len(errors) == 0


class TestMissingReferences:
    """Test detection of references to non-existent entities."""

    def test_valid_references_pass(self):
        """Valid repo and service references should pass."""
        config = load_config(FIXTURES_DIR / "valid_config.yaml")
        errors = check_missing_references(config)
        assert len(errors) == 0

    def test_missing_after_service_detected(self):
        """Reference to non-existent service in after should be detected."""
        config = load_config(FIXTURES_DIR / "invalid_missing_after.yaml")
        errors = check_missing_references(config)
        assert len(errors) > 0
        error_msg = errors[0].message
        assert "non-existent-service" in error_msg

    def test_missing_repo_detected(self):
        """Reference to non-existent repo should be detected."""
        config = load_config(FIXTURES_DIR / "invalid_missing_repo.yaml")
        errors = check_missing_references(config)
        assert len(errors) > 0
        error_msg = errors[0].message
        assert "non-existent-repo" in error_msg


class TestValidationErrorDetails:
    """Test that validation errors provide helpful details."""

    def test_error_has_message(self):
        """Validation errors should have descriptive messages."""
        config = load_config(FIXTURES_DIR / "invalid_circular.yaml")
        errors = validate_config(config)
        assert len(errors) > 0
        assert errors[0].message  # Should not be empty

    def test_error_has_severity(self):
        """Validation errors should have severity level."""
        config = load_config(FIXTURES_DIR / "invalid_circular.yaml")
        errors = validate_config(config)
        assert len(errors) > 0
        assert errors[0].severity in ["error", "warning"]

    def test_error_has_location(self):
        """Validation errors should indicate problematic location."""
        config = load_config(FIXTURES_DIR / "invalid_circular.yaml")
        errors = validate_config(config)
        assert len(errors) > 0
        # Should mention services involved
        assert errors[0].location is not None


class TestInputChecks:
    """Unknown keys and bad values that the models read leniently."""

    @staticmethod
    def _issues(text: str, tmp_path: Path):
        import yaml

        from haniel.config.input_checks import config_input_issues

        path = tmp_path / "haniel.yaml"
        path.write_text(text, encoding="utf-8")
        return config_input_issues(load_config(path), yaml.safe_load(text))

    @staticmethod
    def _errors(issues) -> list[str]:
        return [i.message for i in issues if i.severity == "error"]

    def test_repo_fixtures_have_no_unknown_keys(self):
        import yaml

        from haniel.config.input_checks import config_input_issues

        for name in ("valid_config.yaml", "invalid_circular.yaml"):
            path = FIXTURES_DIR / name
            issues = config_input_issues(
                load_config(path), yaml.safe_load(path.read_text(encoding="utf-8"))
            )
            assert self._errors(issues) == [], name

    def test_unknown_service_key_names_path_and_close_match(self, tmp_path):
        issues = self._issues(
            "services:\n  api:\n    run: x\n    redy: 'port:80'\n", tmp_path
        )
        assert self._errors(issues) == [
            "unknown key services.api.redy (did you mean ready?)"
        ]

    def test_unknown_top_level_key(self, tmp_path):
        issues = self._issues("serivces: {}\n", tmp_path)
        assert self._errors(issues) == ["unknown key serivces (did you mean services?)"]

    def test_unknown_key_without_close_match_has_no_hint(self, tmp_path):
        issues = self._issues("services:\n  a:\n    run: x\n    zzzzzz: 1\n", tmp_path)
        assert self._errors(issues) == ["unknown key services.a.zzzzzz"]

    def test_unknown_keys_in_lists_maps_and_aliased_sections(self, tmp_path):
        issues = self._issues(
            "webhooks:\n  - url: http://x\n    formt: json\n"
            "repos:\n  r:\n    url: u\n    path: p\n    brnch: main\n"
            "self:\n  repo: r\n  auto_updat: true\n"
            "install:\n  configs:\n    c:\n      path: p\n"
            "      keys:\n        - key: k\n          validate_cmd: x\n",
            tmp_path,
        )
        assert self._errors(issues) == [
            "unknown key webhooks.0.formt (did you mean format?)",
            "unknown key repos.r.brnch (did you mean branch?)",
            "unknown key self.auto_updat (did you mean auto_update?)",
            "unknown key install.configs.c.keys.0.validate_cmd (did you mean validate?)",
        ]

    def test_unused_but_known_keys_are_not_unknown(self, tmp_path):
        issues = self._issues(
            "mcp: {transport: streamable_http}\ndashboard: {port: 1}\n"
            "services:\n  a:\n    run: x\n    reflect: true\n",
            tmp_path,
        )
        assert self._errors(issues) == []

    @pytest.mark.parametrize(
        "ready",
        [
            "port:8080",
            "port:1",
            "port:65535",
            "delay:3",
            "delay:0",
            "delay:0.1",
            "log:Ready",
            "http:/health",
            "http:localhost:8080/health",
            "http://localhost:8080/health",
        ],
    )
    def test_usable_ready_values(self, ready):
        from haniel.config.input_checks import ready_format_error

        assert ready_format_error(ready) is None

    @pytest.mark.parametrize(
        "ready",
        [
            "prt:80",
            "80",
            "port:",
            "port:abc",
            "port:0",
            "port:65536",
            "port:-1",
            "delay:",
            "delay:-1",
            "delay:abc",
            "delay:nan",
            "delay:inf",
            "log:",
            "log:   ",
            "http:",
        ],
    )
    def test_unusable_ready_values(self, ready):
        from haniel.config.input_checks import ready_format_error

        assert ready_format_error(ready)

    def test_ready_prefix_typo_suggests_prefix(self):
        from haniel.config.input_checks import ready_format_error

        assert "did you mean port:?" in ready_format_error("prt:80")

    def test_value_errors_name_their_field(self, tmp_path):
        issues = self._issues(
            "shutdown: {signal: SIGTRM}\n"
            "backoff: {base_delay: -1, max_delay: -2}\n"
            "orchestrator_client: {url: '', token: '', node_id: n}\n"
            "repos:\n  r: {url: '', path: ./r}\n"
            "services:\n"
            "  a: {run: x, ready: 'port:0', restart_delay: -5,"
            " shutdown: {signal: sigterm}}\n",
            tmp_path,
        )
        errors = self._errors(issues)
        assert (
            "shutdown.signal: unknown signal 'SIGTRM' (did you mean SIGTERM?)" in errors
        )
        assert "backoff.base_delay must be 0 or more: -1" in errors
        assert "backoff.max_delay must be 0 or more: -2" in errors
        assert "orchestrator_client.url must not be empty" in errors
        assert "orchestrator_client.token must not be empty" in errors
        assert "repos.r.url must not be empty" in errors
        assert any(e.startswith("services.a.ready: ") for e in errors)
        assert "services.a.restart_delay must be 0 or more: -5" in errors
        assert (
            "services.a.shutdown.signal: unknown signal 'sigterm' "
            "(did you mean SIGTERM?)" in errors
        )

    def test_os_signal_names_are_accepted(self, tmp_path):
        issues = self._issues(
            "shutdown: {signal: SIGINT}\nservices:\n  a: {run: x}\n", tmp_path
        )
        assert self._errors(issues) == []

    def test_no_services_is_only_a_warning(self, tmp_path):
        issues = self._issues("poll_interval: 60\n", tmp_path)
        assert [(i.severity, i.message) for i in issues] == [
            ("warning", "no services are defined")
        ]
