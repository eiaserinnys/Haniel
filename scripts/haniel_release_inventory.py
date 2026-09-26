"""Validated active-release inventory and migration operations."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from haniel_release_fs import (
    LEGACY_CURRENT_POINTER,
    ReleaseFilesystemError,
    is_reparse_leaf,
    read_release_pointer,
    release_directory_matches_commit,
    remove_reparse_leaf,
    write_json_atomic,
    write_release_pointer,
)
from haniel_release_policy import BROKEN_MARKER, read_ready_commit


class ReleaseInventoryError(RuntimeError):
    """The active release inventory is missing or unsafe."""


def release_python(release: Path) -> Path:
    if os.name == "nt":
        return release / ".venv" / "Scripts" / "python.exe"
    return release / ".venv" / "bin" / "python"


def _validate_active_release(release_root: Path, active: Path) -> Path:
    try:
        active = active.resolve(strict=True)
        releases = (release_root / "releases").resolve(strict=True)
    except OSError as exc:
        raise ReleaseInventoryError(f"current pointer is broken: {exc}") from exc
    if active.parent != releases:
        raise ReleaseInventoryError(
            f"current pointer escapes release inventory: {active}"
        )
    ready_commit = read_ready_commit(active)
    if (
        ready_commit is None
        or not release_directory_matches_commit(active.name, ready_commit)
        or not release_python(active).is_file()
    ):
        raise ReleaseInventoryError(f"current release is incomplete: {active}")
    return active


def active_release(release_root: Path, *, migrate_legacy: bool = True) -> Path | None:
    """Read the active release, optionally migrating the legacy link."""
    legacy = release_root / LEGACY_CURRENT_POINTER
    try:
        active_name = read_release_pointer(release_root)
    except ReleaseFilesystemError as exc:
        raise ReleaseInventoryError(str(exc)) from exc

    if active_name is not None:
        active = _validate_active_release(
            release_root,
            release_root / "releases" / active_name,
        )
        if migrate_legacy and (
            legacy.exists() or legacy.is_symlink() or is_reparse_leaf(legacy)
        ):
            if not is_reparse_leaf(legacy):
                raise ReleaseInventoryError(
                    f"legacy current pointer is not a reparse leaf: {legacy}"
                )
            remove_reparse_leaf(legacy)
        return active

    if not legacy.exists() and not legacy.is_symlink() and not is_reparse_leaf(legacy):
        return None
    if not is_reparse_leaf(legacy):
        raise ReleaseInventoryError(
            f"legacy current pointer is not a reparse leaf: {legacy}"
        )
    try:
        active = legacy.resolve(strict=True)
    except OSError as exc:
        raise ReleaseInventoryError(f"current pointer is broken: {exc}") from exc
    active = _validate_active_release(release_root, active)
    if not migrate_legacy:
        return active
    try:
        write_release_pointer(release_root, active.name)
        remove_reparse_leaf(legacy)
    except (OSError, ReleaseFilesystemError) as exc:
        raise ReleaseInventoryError(
            f"failed to migrate legacy current pointer: {exc}"
        ) from exc
    return active


def ready_release_for_commit(releases: Path, commit: str) -> Path | None:
    if not releases.is_dir():
        return None
    canonical = releases / commit[:12]
    candidates = [canonical]
    candidates.extend(
        sorted(
            (
                entry
                for entry in releases.iterdir()
                if entry != canonical
                and release_directory_matches_commit(entry.name, commit)
            ),
            key=lambda entry: entry.name,
        )
    )
    for candidate in candidates:
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        if (
            read_ready_commit(candidate) == commit
            and release_python(candidate).is_file()
        ):
            return candidate
    return None


def select_release_path(releases: Path, commit: str) -> Path:
    ready = ready_release_for_commit(releases, commit)
    if ready is not None:
        return ready
    canonical = releases / commit[:12]
    canonical_commit = read_ready_commit(canonical)
    if (canonical / BROKEN_MARKER).is_file() or (
        canonical_commit is not None and canonical_commit != commit
    ):
        return releases / f"{commit[:12]}.retry-{uuid.uuid4().hex[:8]}"
    return canonical


def mark_broken_release(release: Path, commit: str, error: OSError) -> None:
    if not release.is_dir() or release.is_symlink():
        return
    write_json_atomic(
        release / BROKEN_MARKER,
        {
            "version": 1,
            "commit": commit,
            "cleanup_error": str(error),
        },
    )
