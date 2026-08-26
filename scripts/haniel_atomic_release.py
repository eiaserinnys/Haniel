#!/usr/bin/env python3
"""Prepare and atomically activate a commit-specific Haniel release.

This helper intentionally uses only the Python standard library so both outer
wrappers can execute it before the candidate Haniel package is installed.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

from haniel_release_fs import (
    LEGACY_CURRENT_POINTER,
    ReleaseFilesystemError,
    is_reparse_leaf,
    remove_reparse_leaf,
    remove_tree,
    write_json_atomic as _write_json_atomic,
    write_release_pointer,
)
from haniel_release_inventory import (
    ReleaseInventoryError,
    active_release as _inventory_active_release,
    mark_broken_release as _mark_broken_release,
    release_python as _release_python,
    select_release_path as _select_release_path,
)

from haniel_release_policy import (
    BROKEN_MARKER as BROKEN_MARKER,
    COMMIT_PATTERN,
    DEFAULT_MIN_FREE_MB,
    DEFAULT_RETAIN_EXTRA,
    READY_MARKER,
    InsufficientDiskSpace,
    ReleasePolicyError,
    read_ready_commit,
    require_disk_space,
)
from haniel_release_prune import prune, queue_prune_request
from haniel_release_steps import (
    PreparationResult,
    ReleasePreparationError,
    command_error as _command_error,
    elapsed_since,
    monotonic_time,
    run_step as _run_step,
)


def _git_output(source: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(source), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ReleasePreparationError(_command_error(completed))
    return completed.stdout.strip()


def _resolve_commit(source: Path, ref: str) -> str:
    commit = _git_output(source, "rev-parse", f"{ref}^{{commit}}")
    if not COMMIT_PATTERN.fullmatch(commit):
        raise ReleasePreparationError(f"invalid commit resolved from {ref}: {commit}")
    return commit


def _detect_branch(source: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(source), "symbolic-ref", "--short", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    branch = completed.stdout.strip()
    return branch if completed.returncode == 0 and branch else "main"


def _active_release(release_root: Path) -> Path | None:
    try:
        return _inventory_active_release(release_root)
    except ReleaseInventoryError as exc:
        raise ReleasePreparationError(str(exc)) from exc


def _require_disk_space(
    result: PreparationResult,
    release_root: Path,
    min_free_mb: int,
) -> None:
    started_at = monotonic_time()
    try:
        require_disk_space(release_root, min_free_mb)
    except InsufficientDiskSpace as exc:
        result.error_code = "insufficient_disk_space"
        result.add_step(
            "disk_space", False, str(exc), duration_sec=elapsed_since(started_at)
        )
        raise ReleasePreparationError(result.error or str(exc)) from exc
    except ReleasePolicyError as exc:
        result.add_step(
            "disk_space", False, str(exc), duration_sec=elapsed_since(started_at)
        )
        raise ReleasePreparationError(result.error or str(exc)) from exc
    result.add_step("disk_space", True, duration_sec=elapsed_since(started_at))


def _prepare_release(
    result: PreparationResult,
    *,
    source: Path,
    releases: Path,
    commit: str,
    bootstrap_python: Path,
    min_free_mb: int,
) -> Path:
    release_started_at = monotonic_time()
    if not COMMIT_PATTERN.fullmatch(commit):
        raise ReleasePreparationError(f"unsafe release commit: {commit}")
    release = _select_release_path(releases, commit)
    if read_ready_commit(release) == commit and _release_python(release).is_file():
        result.add_step(
            "release_reuse", True, duration_sec=elapsed_since(release_started_at)
        )
        return release

    _require_disk_space(result, releases.parent, min_free_mb)

    if release.exists() or release.is_symlink():
        if release.is_symlink() or not release.is_dir():
            raise ReleasePreparationError(f"unsafe release path: {release}")
        try:
            remove_tree(release)
        except OSError as exc:
            _mark_broken_release(release, commit, exc)
            raise ReleasePreparationError(
                f"failed to remove incomplete release {release}: {exc}"
            ) from exc

    releases.mkdir(parents=True, exist_ok=True)
    try:
        _run_step(
            result,
            "release_checkout",
            [
                "git",
                "clone",
                "--quiet",
                "--no-hardlinks",
                "--no-checkout",
                str(source),
                str(release),
            ],
        )
        _run_step(
            result,
            "release_checkout_commit",
            ["git", "-C", str(release), "checkout", "--quiet", "--detach", commit],
        )
        _run_step(
            result,
            "venv_create",
            [str(bootstrap_python), "-m", "venv", str(release / ".venv")],
        )
        release_python = _release_python(release)
        _run_step(
            result,
            "pip_install",
            [str(release_python), "-m", "pip", "install", "-e", str(release)],
        )
        _run_step(
            result,
            "import_smoke",
            [
                str(release_python),
                "-m",
                "haniel.integrations.mcp_compatibility",
            ],
        )
        dashboard = release / "dashboard"
        if dashboard.is_dir():
            pnpm_started_at = monotonic_time()
            pnpm = shutil.which("pnpm")
            if pnpm is None:
                result.add_step(
                    "pnpm_install",
                    False,
                    "pnpm not found",
                    duration_sec=elapsed_since(pnpm_started_at),
                )
                raise ReleasePreparationError(result.error or "pnpm not found")
            _run_step(
                result,
                "pnpm_install",
                [pnpm, "--dir", str(dashboard), "install"],
            )
            _run_step(
                result,
                "pnpm_build",
                [pnpm, "--dir", str(dashboard), "build"],
            )
        ready_started_at = monotonic_time()
        try:
            _write_json_atomic(
                release / READY_MARKER,
                {"version": 1, "commit": commit},
            )
        except OSError as exc:
            result.add_step(
                "release_ready",
                False,
                str(exc),
                duration_sec=elapsed_since(ready_started_at),
            )
            raise
        result.add_step(
            "release_ready", True, duration_sec=elapsed_since(ready_started_at)
        )
        return release
    except Exception:
        if release.is_dir() and not release.is_symlink():
            cleanup_started_at = monotonic_time()
            try:
                remove_tree(release)
            except OSError as cleanup_error:
                _mark_broken_release(release, commit, cleanup_error)
                result.add_step(
                    "release_cleanup",
                    False,
                    str(cleanup_error),
                    duration_sec=elapsed_since(cleanup_started_at),
                )
            else:
                result.add_step(
                    "release_cleanup",
                    True,
                    duration_sec=elapsed_since(cleanup_started_at),
                )
        raise


def _switch_current(
    result: PreparationResult,
    release_root: Path,
    release: Path,
    *,
    previous: Path | None,
    retain_extra: int,
) -> None:
    started_at = monotonic_time()
    try:
        write_release_pointer(release_root, release.name)
        legacy = release_root / LEGACY_CURRENT_POINTER
        if legacy.exists() or legacy.is_symlink() or is_reparse_leaf(legacy):
            if not is_reparse_leaf(legacy):
                raise ReleaseFilesystemError(
                    f"legacy current pointer is not a reparse leaf: {legacy}"
                )
            remove_reparse_leaf(legacy)
    except (OSError, ReleaseFilesystemError) as exc:
        result.add_step(
            "current_switch", False, str(exc), duration_sec=elapsed_since(started_at)
        )
        raise ReleasePreparationError(result.error or str(exc)) from exc
    if _active_release(release_root) != release.resolve(strict=True):
        result.add_step(
            "current_switch",
            False,
            "post-switch target mismatch",
            duration_sec=elapsed_since(started_at),
        )
        raise ReleasePreparationError(result.error or "post-switch target mismatch")
    result.switched = True
    try:
        queue_prune_request(
            release_root,
            current=release,
            previous=previous,
            retain_extra=retain_extra,
        )
    except OSError as exc:
        result.warnings.append(f"release cleanup request failed: {exc}")
    result.add_step(
        "current_switch", True, duration_sec=elapsed_since(started_at)
    )


def _fetch_target(
    result: PreparationResult, source: Path, branch: str, max_failures: int
) -> str:
    started_at = monotonic_time()
    last_error = ""
    for attempt in range(1, max_failures + 1):
        completed = subprocess.run(
            ["git", "-C", str(source), "fetch", "origin"],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode == 0:
            result.add_step(
                "git_fetch", True, duration_sec=elapsed_since(started_at)
            )
            return _resolve_commit(source, f"origin/{branch}")
        last_error = _command_error(completed)
        print(
            f"[haniel-release] git fetch failed "
            f"(attempt {attempt}/{max_failures}): {last_error}",
            file=sys.stderr,
        )
        if attempt < max_failures:
            time.sleep(5)
    result.add_step(
        "git_fetch",
        False,
        last_error or "git fetch failed",
        duration_sec=elapsed_since(started_at),
    )
    raise ReleasePreparationError(result.error or "git fetch failed")


def prepare(args: argparse.Namespace) -> PreparationResult:
    source = Path(args.source).resolve(strict=True)
    release_root = Path(args.release_root).resolve()
    bootstrap_python = Path(args.bootstrap_python).resolve(strict=True)
    result = PreparationResult()
    releases = release_root / "releases"

    try:
        source_head = _resolve_commit(source, "HEAD")
        active = _active_release(release_root)
        if active is None:
            baseline = source_head
            if args.prefer_orig_head:
                try:
                    candidate = _resolve_commit(source, "ORIG_HEAD")
                except ReleasePreparationError:
                    candidate = source_head
                if candidate != source_head:
                    baseline = candidate
            baseline_release = _prepare_release(
                result,
                source=source,
                releases=releases,
                commit=baseline,
                bootstrap_python=bootstrap_python,
                min_free_mb=args.min_free_mb,
            )
            _switch_current(
                result,
                release_root,
                baseline_release,
                previous=None,
                retain_extra=args.retain_extra,
            )
            active = baseline_release
            result.migrated = True
            print(
                "[haniel-release] Migrated legacy checkout into release baseline "
                f"{baseline[:12]}."
            )

        branch = _detect_branch(source)
        target = _fetch_target(result, source, branch, args.max_git_failures)
        result.target_commit = target
        active_commit = read_ready_commit(active)
        assert active_commit is not None
        if active_commit != target:
            candidate = _prepare_release(
                result,
                source=source,
                releases=releases,
                commit=target,
                bootstrap_python=bootstrap_python,
                min_free_mb=args.min_free_mb,
            )
            previous = active
            _switch_current(
                result,
                release_root,
                candidate,
                previous=previous,
                retain_extra=args.retain_extra,
            )
            active = candidate
        else:
            reuse_started_at = monotonic_time()
            result.add_step(
                "release_reuse",
                True,
                duration_sec=elapsed_since(reuse_started_at),
            )

        result.active_repo = str(active)
        result.active_python = str(_release_python(active))
        result.active_commit = read_ready_commit(active)
        result.ok = True
        return result
    except Exception as exc:  # noqa: BLE001 - preserve active release on gate failure
        if result.error is None:
            result.error = str(exc)
        try:
            active = _active_release(release_root)
        except ReleasePreparationError:
            active = None
        if active is not None:
            result.active_repo = str(active)
            result.active_python = str(_release_python(active))
            result.active_commit = read_ready_commit(active)
        return result


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source")
    parser.add_argument("--release-root", required=True)
    parser.add_argument("--bootstrap-python")
    parser.add_argument("--result-json", required=True)
    parser.add_argument("--prune-only", action="store_true")
    parser.add_argument("--max-git-failures", type=_positive_int, default=3)
    parser.add_argument(
        "--retain-extra",
        type=_non_negative_int,
        default=DEFAULT_RETAIN_EXTRA,
    )
    parser.add_argument(
        "--min-free-mb",
        type=_positive_int,
        default=DEFAULT_MIN_FREE_MB,
    )
    parser.add_argument("--prefer-orig-head", action="store_true")
    args = parser.parse_args()
    if not args.prune_only:
        if args.source is None:
            parser.error("--source is required unless --prune-only is used")
        if args.bootstrap_python is None:
            parser.error(
                "--bootstrap-python is required unless --prune-only is used"
            )
    return args


def main() -> int:
    args = parse_args()
    result_path = Path(args.result_json)
    try:
        result = prune(args.release_root) if args.prune_only else prepare(args)
    except Exception as exc:  # noqa: BLE001 - always emit a machine-readable failure result
        result = PreparationResult(error=f"release helper crashed: {exc}")
    try:
        _write_json_atomic(result_path, result.as_dict())
    except OSError as exc:
        print(f"[haniel-release] Failed to write result: {exc}", file=sys.stderr)
        return 1
    if not result.ok:
        print(f"[haniel-release] {result.error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
