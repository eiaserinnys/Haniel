"""
haniel configuration validators.

This module provides validation functions that check semantic correctness
of haniel.yaml configurations beyond basic schema validation.

Validations performed:
- Circular dependencies in service 'after' fields
- Port conflicts in 'ready: port:*' conditions
- Duplicate repository paths
- Missing references (non-existent services/repos)
"""

from dataclasses import dataclass
from typing import Literal
import re

from haniel.config import HanielConfig


@dataclass
class ValidationError:
    """Represents a validation error with details."""

    message: str
    severity: Literal["error", "warning"]
    location: str | None = None

    def __str__(self) -> str:
        if self.location:
            return f"[{self.severity.upper()}] {self.location}: {self.message}"
        return f"[{self.severity.upper()}] {self.message}"


def validate_config(config: HanielConfig) -> list[ValidationError]:
    """Run all validation checks on a configuration.

    Args:
        config: Validated HanielConfig instance

    Returns:
        List of validation errors (empty if all checks pass)
    """
    errors: list[ValidationError] = []

    errors.extend(check_circular_dependencies(config))
    errors.extend(check_port_conflicts(config))
    errors.extend(check_duplicate_paths(config))
    errors.extend(check_missing_references(config))

    return errors


def check_circular_dependencies(config: HanielConfig) -> list[ValidationError]:
    """Check for circular dependencies in service 'after' fields.

    Uses DFS to detect cycles in the dependency graph.

    Args:
        config: HanielConfig instance

    Returns:
        List of validation errors for any cycles found
    """
    errors: list[ValidationError] = []

    # Build adjacency list: service -> services it depends on
    graph: dict[str, list[str]] = {}
    for name, service in config.services.items():
        graph[name] = service.after

    # Track visit state: 0 = unvisited, 1 = visiting (in current path), 2 = visited
    state: dict[str, int] = {name: 0 for name in graph}

    def dfs(node: str, path: list[str]) -> list[str] | None:
        """DFS that returns the cycle path if found."""
        if state.get(node, 0) == 1:
            # Found a cycle - return the path from this node
            cycle_start = path.index(node)
            return path[cycle_start:] + [node]

        if state.get(node, 0) == 2:
            return None

        if node not in graph:
            # Reference to non-existent service - handled by check_missing_references
            return None

        state[node] = 1
        path.append(node)

        for dep in graph.get(node, []):
            result = dfs(dep, path)
            if result:
                return result

        path.pop()
        state[node] = 2
        return None

    for service_name in graph:
        if state[service_name] == 0:
            cycle = dfs(service_name, [])
            if cycle:
                cycle_str = " -> ".join(cycle)
                errors.append(ValidationError(
                    message=f"Circular dependency detected: {cycle_str}",
                    severity="error",
                    location=f"services.{service_name}"
                ))
                # Reset state for potentially finding more cycles
                for name in graph:
                    if state[name] == 1:
                        state[name] = 0

    return errors


def check_port_conflicts(config: HanielConfig) -> list[ValidationError]:
    """Check for port conflicts in 'ready: port:*' conditions.

    Multiple services using the same port for ready checks indicates a conflict.

    Args:
        config: HanielConfig instance

    Returns:
        List of validation errors for any port conflicts
    """
    errors: list[ValidationError] = []

    # Extract ports from ready conditions
    port_pattern = re.compile(r"^port:(\d+)$")
    port_users: dict[int, list[str]] = {}

    for name, service in config.services.items():
        if service.ready:
            match = port_pattern.match(service.ready)
            if match:
                port = int(match.group(1))
                if port not in port_users:
                    port_users[port] = []
                port_users[port].append(name)

    # Find conflicts
    for port, services in port_users.items():
        if len(services) > 1:
            services_str = ", ".join(services)
            errors.append(ValidationError(
                message=f"Port {port} is used by multiple services: {services_str}",
                severity="error",
                location=f"services (port {port})"
            ))

    return errors


def check_duplicate_paths(config: HanielConfig) -> list[ValidationError]:
    """Check for duplicate repository paths.

    Multiple repos pointing to the same path would cause conflicts.

    Args:
        config: HanielConfig instance

    Returns:
        List of validation errors for any duplicate paths
    """
    errors: list[ValidationError] = []

    path_users: dict[str, list[str]] = {}

    for name, repo in config.repos.items():
        path = repo.path
        # Normalize path for comparison
        normalized = path.rstrip("/\\")
        if normalized not in path_users:
            path_users[normalized] = []
        path_users[normalized].append(name)

    for path, repos in path_users.items():
        if len(repos) > 1:
            repos_str = ", ".join(repos)
            errors.append(ValidationError(
                message=f"Duplicate repository path '{path}' used by: {repos_str}",
                severity="error",
                location=f"repos ({repos_str})"
            ))

    return errors


def check_missing_references(config: HanielConfig) -> list[ValidationError]:
    """Check for references to non-existent services or repos.

    Validates:
    - service.after references exist
    - service.repo references exist

    Args:
        config: HanielConfig instance

    Returns:
        List of validation errors for any missing references
    """
    errors: list[ValidationError] = []

    service_names = set(config.services.keys())
    repo_names = set(config.repos.keys())

    for name, service in config.services.items():
        # Check after references
        for dep in service.after:
            if dep not in service_names:
                errors.append(ValidationError(
                    message=f"Service '{name}' references non-existent service '{dep}' in 'after'",
                    severity="error",
                    location=f"services.{name}.after"
                ))

        # Check repo references
        if service.repo and service.repo not in repo_names:
            errors.append(ValidationError(
                message=f"Service '{name}' references non-existent repo '{service.repo}'",
                severity="error",
                location=f"services.{name}.repo"
            ))

    return errors
