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
    ValidationError as HanielValidationError,
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
        assert any("path" in e.message.lower() or "duplicate" in e.message.lower() for e in errors)


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
            services={
                "self-ref": ServiceConfig(
                    run="echo hello",
                    after=["self-ref"]
                )
            }
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
            }
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
