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
    ServiceConfig,
    ServiceDefinitionConfig,
    ServiceShutdownConfig,
    ShutdownConfig,
    WebhookConfig,
    load_config,
)
from .validators import ValidationError, validate_config

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
    "ServiceConfig",
    "ServiceDefinitionConfig",
    "ServiceShutdownConfig",
    "ShutdownConfig",
    "WebhookConfig",
    "load_config",
    # validators
    "ValidationError",
    "validate_config",
]
