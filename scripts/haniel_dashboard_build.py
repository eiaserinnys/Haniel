"""Persistent dashboard build workspace for atomic Haniel releases."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from haniel_release_fs import is_reparse_leaf, remove_reparse_leaf, remove_tree
from haniel_release_steps import (
    PreparationResult,
    ReleasePreparationError,
    command_error,
    elapsed_since,
    monotonic_time,
    run_step,
)


DASHBOARD_BUILD_DIRECTORY = "dashboard-build"


def _remove_path(path: Path) -> None:
    if is_reparse_leaf(path):
        remove_reparse_leaf(path)
    elif path.is_dir():
        remove_tree(path)
    else:
        path.unlink()


def _dashboard_tree(source: Path, commit: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(source), "rev-parse", f"{commit}:dashboard"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ReleasePreparationError(command_error(completed))
    return completed.stdout.strip()


def _seed_node_modules(
    result: PreparationResult,
    *,
    build_dir: Path,
    active_release: Path | None,
) -> None:
    if active_release is None:
        return
    source = active_release / "dashboard" / "node_modules"
    destination = build_dir / "node_modules"
    if destination.exists() or destination.is_symlink() or not source.is_dir():
        return

    started_at = monotonic_time()
    try:
        build_dir.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
    except OSError as exc:
        result.add_step(
            "dashboard_node_modules_seed",
            False,
            str(exc),
            duration_sec=elapsed_since(started_at),
        )
        raise ReleasePreparationError(result.error or str(exc)) from exc
    result.add_step(
        "dashboard_node_modules_seed",
        True,
        duration_sec=elapsed_since(started_at),
        detail=f"moved node_modules from {active_release} into persistent dir",
    )


def _sync_dashboard_sources(
    result: PreparationResult,
    *,
    dashboard: Path,
    build_dir: Path,
) -> None:
    started_at = monotonic_time()
    try:
        build_dir.mkdir(parents=True, exist_ok=True)
        for entry in build_dir.iterdir():
            if entry.name == "node_modules":
                continue
            _remove_path(entry)
        for entry in dashboard.iterdir():
            destination = build_dir / entry.name
            if entry.is_dir():
                shutil.copytree(entry, destination)
            else:
                shutil.copy2(entry, destination)
    except OSError as exc:
        result.add_step(
            "dashboard_sync",
            False,
            str(exc),
            duration_sec=elapsed_since(started_at),
        )
        raise ReleasePreparationError(result.error or str(exc)) from exc
    result.add_step(
        "dashboard_sync", True, duration_sec=elapsed_since(started_at)
    )


def _copy_dashboard_dist(
    result: PreparationResult,
    *,
    source_dist: Path,
    release_dashboard: Path,
    detail: str,
) -> None:
    started_at = monotonic_time()
    destination = release_dashboard / "dist"
    try:
        if not (source_dist / "index.html").is_file():
            raise OSError(f"dashboard dist has no index.html: {source_dist}")
        if destination.exists() or destination.is_symlink():
            _remove_path(destination)
        shutil.copytree(source_dist, destination)
    except OSError as exc:
        result.add_step(
            "dashboard_dist_copy",
            False,
            str(exc),
            duration_sec=elapsed_since(started_at),
        )
        raise ReleasePreparationError(result.error or str(exc)) from exc
    result.add_step(
        "dashboard_dist_copy",
        True,
        duration_sec=elapsed_since(started_at),
        detail=detail,
    )


def prepare_dashboard(
    result: PreparationResult,
    *,
    source: Path,
    release_root: Path,
    release: Path,
    commit: str,
    active_release: Path | None,
    active_commit: str | None,
) -> None:
    """Reuse an unchanged dashboard or build it in one persistent directory."""
    dashboard = release / "dashboard"
    if not dashboard.is_dir():
        return

    build_dir = release_root / DASHBOARD_BUILD_DIRECTORY
    _seed_node_modules(
        result,
        build_dir=build_dir,
        active_release=active_release,
    )

    if active_release is not None and active_commit is not None:
        compare_started_at = monotonic_time()
        try:
            unchanged = _dashboard_tree(source, active_commit) == _dashboard_tree(
                source, commit
            )
        except ReleasePreparationError as exc:
            result.add_step(
                "dashboard_compare",
                False,
                str(exc),
                duration_sec=elapsed_since(compare_started_at),
            )
            raise
        result.add_step(
            "dashboard_compare",
            True,
            duration_sec=elapsed_since(compare_started_at),
            detail="dashboard unchanged" if unchanged else "dashboard changed",
        )
        if unchanged:
            _copy_dashboard_dist(
                result,
                source_dist=active_release / "dashboard" / "dist",
                release_dashboard=dashboard,
                detail=f"dashboard unchanged, reused dist from {active_release}",
            )
            return

    _sync_dashboard_sources(result, dashboard=dashboard, build_dir=build_dir)
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
    run_step(
        result,
        "pnpm_install",
        [
            pnpm,
            "--dir",
            str(build_dir),
            "install",
            "--frozen-lockfile",
            "--prefer-offline",
        ],
    )
    run_step(
        result,
        "pnpm_build",
        [pnpm, "--dir", str(build_dir), "build"],
    )
    _copy_dashboard_dist(
        result,
        source_dist=build_dir / "dist",
        release_dashboard=dashboard,
        detail=f"built in persistent dir {build_dir}",
    )
