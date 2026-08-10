"""
haniel configuration — schema, parsing, and validation.

Re-exports all public symbols from submodules for backward compatibility:
    from haniel.config import HanielConfig, load_config  # works
    from haniel.config import validate_config             # works
"""

from .model import (
    BackoffConfig,
    ConfigFileConfig,
    ConfigKeyConfig,
    DashboardConfig,
    EnvironmentConfig,
    HanielConfig,
    HooksConfig,
    InstallConfig,
    McpConfig,
    RepoConfig,
    SelfUpdateConfig,
    ServiceAccountConfig,
    ServiceConfig,
    ServiceDefinitionConfig,
    ServiceShutdownConfig,
    ShutdownConfig,
    WebhookConfig,
    load_config,
)
from .readiness import (
    ReadinessConfigError,
    ReadyCondition,
    ReadyConditionType,
    parse_ready_condition,
    ready_port,
)
from .validators import (
    ConfigSemanticError,
    ConfigValidationEvidence,
    ValidationError,
    require_valid_config,
    validate_config,
)

__all__ = [
    # model
    "BackoffConfig",
    "DashboardConfig",
    "ConfigFileConfig",
    "ConfigKeyConfig",
    "EnvironmentConfig",
    "HanielConfig",
    "HooksConfig",
    "InstallConfig",
    "McpConfig",
    "RepoConfig",
    "SelfUpdateConfig",
    "ServiceAccountConfig",
    "ServiceConfig",
    "ServiceDefinitionConfig",
    "ServiceShutdownConfig",
    "ShutdownConfig",
    "WebhookConfig",
    "load_config",
    "ReadinessConfigError",
    "ReadyCondition",
    "ReadyConditionType",
    "parse_ready_condition",
    "ready_port",
    # validators
    "ConfigSemanticError",
    "ConfigValidationEvidence",
    "ValidationError",
    "require_valid_config",
    "validate_config",
]
