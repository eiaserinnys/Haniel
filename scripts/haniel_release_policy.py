"""Disk and retention policy for commit-specific Haniel releases."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

READY_MARKER = ".haniel-release-ready.json"
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
DEFAULT_RETAIN_EXTRA = 3
DEFAULT_MIN_FREE_MB = 5120
MIB = 1024 * 1024


class ReleasePolicyError(RuntimeError):
    """The release inventory cannot be handled safely."""


class InsufficientDiskSpace(ReleasePolicyError):
    """The release filesystem has less free space than the configured floor."""

    def __init__(self, *, free_mb: int, min_free_mb: int, usage_path: Path) -> None:
        self.free_mb = free_mb
        self.min_free_mb = min_free_mb
        self.usage_path = usage_path
        super().__init__(
            f"insufficient disk space: {free_mb} MiB free at {usage_path}; "
            f"at least {min_free_mb} MiB required"
        )


@dataclass(frozen=True)
class PruneOutcome:
    deleted: tuple[str, ...]
    failures: tuple[str, ...]


def read_ready_commit(release: Path) -> str | None:
    marker = release / READY_MARKER
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    commit = payload.get("commit")
    if payload.get("version") != 1 or not isinstance(commit, str):
        return None
    return commit if COMMIT_PATTERN.fullmatch(commit) else None


def require_disk_space(release_root: Path, min_free_mb: int) -> None:
    usage_path = release_root
    while not usage_path.exists():
        parent = usage_path.parent
        if parent == usage_path:
            raise ReleasePolicyError(
                f"cannot find existing parent for disk check: {release_root}"
            )
        usage_path = parent

    free_mb = shutil.disk_usage(usage_path).free // MIB
    if free_mb < min_free_mb:
        raise InsufficientDiskSpace(
            free_mb=free_mb,
            min_free_mb=min_free_mb,
            usage_path=usage_path,
        )


def prune_ready_releases(
    release_root: Path,
    *,
    current: Path,
    previous: Path | None,
    retain_extra: int,
) -> PruneOutcome:
    releases = (release_root / "releases").resolve(strict=True)
    current_pointer = release_root / "current"
    if not current_pointer.is_symlink():
        raise ReleasePolicyError("current pointer is not a symlink during cleanup")
    active = current_pointer.resolve(strict=True)
    expected_current = current.resolve(strict=True)
    if active != expected_current or active.parent != releases:
        raise ReleasePolicyError("current pointer changed or escaped during cleanup")

    protected = {active}
    if previous is not None:
        protected.add(previous.resolve(strict=True))

    candidates: list[tuple[int, str, Path]] = []
    for entry in releases.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            continue
        resolved = entry.resolve()
        if resolved.parent != releases or resolved in protected:
            continue
        if read_ready_commit(entry) != entry.name:
            continue
        try:
            modified_ns = (entry / READY_MARKER).stat().st_mtime_ns
        except OSError:
            continue
        candidates.append((modified_ns, entry.name, entry))

    candidates.sort(reverse=True)
    deleted: list[str] = []
    failures: list[str] = []
    for _, name, release in candidates[retain_extra:]:
        try:
            shutil.rmtree(release)
            deleted.append(name)
        except OSError as exc:
            failures.append(f"{name}: {exc}")
    return PruneOutcome(tuple(deleted), tuple(failures))
