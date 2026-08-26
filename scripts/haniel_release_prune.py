"""Durable, post-launch pruning for commit-specific releases."""

from __future__ import annotations

import json
from pathlib import Path

from haniel_release_fs import (
    ReleaseFilesystemError,
    read_release_pointer,
    release_directory_matches_commit,
    write_json_atomic,
)
from haniel_release_policy import (
    ReleasePolicyError,
    prune_ready_releases,
    read_ready_commit,
)
from haniel_release_steps import PreparationResult, elapsed_since, monotonic_time


PRUNE_REQUEST = ".haniel-release-prune-request.json"


def queue_prune_request(
    release_root: Path,
    *,
    current: Path,
    previous: Path | None,
    retain_extra: int,
) -> None:
    write_json_atomic(
        release_root / PRUNE_REQUEST,
        {
            "version": 1,
            "current": current.name,
            "previous": previous.name if previous is not None else None,
            "retain_extra": retain_extra,
        },
    )


def _request_release(
    release_root: Path,
    release_name: str,
    *,
    label: str,
) -> Path:
    release = release_root / "releases" / release_name
    commit = read_ready_commit(release)
    if commit is None or not release_directory_matches_commit(release_name, commit):
        raise ReleasePolicyError(f"prune request {label} release is invalid")
    return release


def _record_prune(
    result: PreparationResult,
    release_root: Path,
    *,
    current: Path,
    previous: Path | None,
    retain_extra: int,
) -> None:
    started_at = monotonic_time()
    try:
        outcome = prune_ready_releases(
            release_root,
            current=current,
            previous=previous,
            retain_extra=retain_extra,
        )
    except (OSError, ReleasePolicyError) as exc:
        warning = f"release cleanup failed: {exc}"
        result.add_step(
            "release_prune",
            False,
            warning,
            duration_sec=elapsed_since(started_at),
        )
        result.warnings.append(warning)
        return
    if outcome.failures:
        warning = "release cleanup failed: " + "; ".join(outcome.failures)
        result.add_step(
            "release_prune",
            False,
            warning,
            duration_sec=elapsed_since(started_at),
        )
        result.warnings.append(warning)
        return
    result.add_step("release_prune", True, duration_sec=elapsed_since(started_at))


def prune(release_root_value: str) -> PreparationResult:
    """Consume one durable prune request without affecting launch success."""
    release_root = Path(release_root_value).resolve()
    request_path = release_root / PRUNE_REQUEST
    result = PreparationResult()
    if not request_path.is_file():
        result.ok = True
        return result

    started_at = monotonic_time()
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ReleasePolicyError("invalid release cleanup request version")
        current_name = payload.get("current")
        previous_name = payload.get("previous")
        retain_extra = payload.get("retain_extra")
        if not isinstance(current_name, str):
            raise ReleasePolicyError("release cleanup request has no current release")
        if previous_name is not None and not isinstance(previous_name, str):
            raise ReleasePolicyError("release cleanup request previous is invalid")
        if not isinstance(retain_extra, int) or retain_extra < 0:
            raise ReleasePolicyError("release cleanup request retention is invalid")
        if read_release_pointer(release_root) != current_name:
            raise ReleasePolicyError("release cleanup request is stale")

        current = _request_release(release_root, current_name, label="current")
        previous = (
            _request_release(release_root, previous_name, label="previous")
            if previous_name is not None
            else None
        )
        _record_prune(
            result,
            release_root,
            current=current,
            previous=previous,
            retain_extra=retain_extra,
        )
    except (
        OSError,
        ValueError,
        TypeError,
        ReleaseFilesystemError,
        ReleasePolicyError,
    ) as exc:
        warning = f"release cleanup failed: {exc}"
        result.add_step(
            "release_prune",
            False,
            warning,
            duration_sec=elapsed_since(started_at),
        )
        result.warnings.append(warning)
    finally:
        try:
            request_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            result.warnings.append(f"release cleanup request removal failed: {exc}")
    result.ok = True
    return result
