"""Path-filtered service restart planning for manifest deployments.

A release manifest may declare, per service, repository paths whose changes
never require restarting that service. This module turns one commit diff into
the set of affected services a handover may keep running.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase

from .release_manifest import ServiceRestartFilter


def paths_all_ignored(
    changed_paths: Sequence[str], ignore_paths: Sequence[str]
) -> bool:
    """Return True when every changed path matches one declared pattern.

    ``fnmatchcase`` treats ``*`` as matching ``/`` too, which keeps patterns
    simple while still erring toward a restart for any unlisted path.
    """
    return bool(changed_paths) and all(
        any(fnmatchcase(path, pattern) for pattern in ignore_paths)
        for path in changed_paths
    )


@dataclass(frozen=True)
class RestartFilterCandidates:
    """Affected services whose restart one deployment may skip."""

    services: frozenset[str]
    changed_path_count: int


def plan_restart_skips(
    *,
    affected: Sequence[str],
    filters: Mapping[str, ServiceRestartFilter],
    changed_paths: Sequence[str],
    dependencies: Mapping[str, Sequence[str]],
    overlapping: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Return affected services that can stay running for this deployment.

    A service is a candidate when the manifest declares a filter for it and
    every changed path matches one of its ``ignore_paths``. Candidates whose
    ``after`` dependency is an affected service that still restarts are
    dropped to a fixpoint, because that dependency coming back up restarts
    its dependents today.
    """
    if not changed_paths:
        return frozenset()
    services = set(affected)
    candidates = {
        name
        for name in services
        if name in filters
        and paths_all_ignored(changed_paths, filters[name].ignore_paths)
    }
    changed = True
    while changed:
        changed = False
        for name in sorted(candidates):
            if any(
                dependency in services
                and dependency not in candidates
                and dependency not in overlapping
                for dependency in dependencies.get(name, ())
            ):
                candidates.discard(name)
                changed = True
    return frozenset(candidates)
