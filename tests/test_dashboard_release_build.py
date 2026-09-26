"""Contracts for reusing Haniel dashboard build inputs and outputs."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _import_script(monkeypatch: pytest.MonkeyPatch, name: str):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    return importlib.import_module(name)


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _source_repo(tmp_path: Path, *, dashboard_changed: bool) -> tuple[Path, str, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "Haniel Test")
    _git(source, "config", "user.email", "haniel-test@example.com")
    dashboard = source / "dashboard"
    dashboard.mkdir()
    (dashboard / "package.json").write_text('{"name":"dashboard"}\n', encoding="utf-8")
    (dashboard / "app.ts").write_text("export const version = 1\n", encoding="utf-8")
    (source / "version.txt").write_text("one\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "active")
    active_commit = _git(source, "rev-parse", "HEAD")

    if dashboard_changed:
        (dashboard / "app.ts").write_text(
            "export const version = 2\n", encoding="utf-8"
        )
    else:
        (source / "version.txt").write_text("two\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "target")
    target_commit = _git(source, "rev-parse", "HEAD")
    return source, active_commit, target_commit


def _result(monkeypatch: pytest.MonkeyPatch):
    steps = _import_script(monkeypatch, "haniel_release_steps")
    return steps.PreparationResult()


def test_unchanged_dashboard_reuses_dist_without_pnpm_and_seeds_node_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dashboard_build = _import_script(monkeypatch, "haniel_dashboard_build")
    source, active_commit, target_commit = _source_repo(
        tmp_path, dashboard_changed=False
    )
    release_root = tmp_path / "release-root"
    active = release_root / "releases" / active_commit[:12]
    target = release_root / "releases" / target_commit[:12]
    (active / "dashboard" / "dist").mkdir(parents=True)
    (active / "dashboard" / "dist" / "index.html").write_text(
        "active dashboard", encoding="utf-8"
    )
    active_modules = active / "dashboard" / "node_modules"
    active_modules.mkdir()
    (active_modules / ".seed-sentinel").write_text("seed", encoding="utf-8")
    shutil.copytree(source / "dashboard", target / "dashboard")
    monkeypatch.setattr(
        dashboard_build.shutil,
        "which",
        lambda _name: pytest.fail("pnpm must not be resolved for unchanged dashboard"),
    )
    result = _result(monkeypatch)

    dashboard_build.prepare_dashboard(
        result,
        source=source,
        release_root=release_root,
        release=target,
        commit=target_commit,
        active_release=active,
        active_commit=active_commit,
    )

    assert (target / "dashboard" / "dist" / "index.html").read_text(
        encoding="utf-8"
    ) == "active dashboard"
    assert not active_modules.exists()
    assert (
        release_root / "dashboard-build" / "node_modules" / ".seed-sentinel"
    ).is_file()
    assert result.steps[-1]["detail"] == (
        f"dashboard unchanged, reused dist from {active}"
    )
    assert all(
        step["name"] not in {"pnpm_install", "pnpm_build"} for step in result.steps
    )


def test_changed_dashboard_builds_in_persistent_dir_and_preserves_node_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dashboard_build = _import_script(monkeypatch, "haniel_dashboard_build")
    source, active_commit, target_commit = _source_repo(
        tmp_path, dashboard_changed=True
    )
    release_root = tmp_path / "release-root"
    active = release_root / "releases" / active_commit[:12]
    target = release_root / "releases" / target_commit[:12]
    (active / "dashboard" / "dist").mkdir(parents=True)
    (active / "dashboard" / "dist" / "index.html").write_text(
        "active dashboard", encoding="utf-8"
    )
    shutil.copytree(source / "dashboard", target / "dashboard")
    build_dir = release_root / "dashboard-build"
    modules = build_dir / "node_modules"
    modules.mkdir(parents=True)
    sentinel = modules / ".persistent-sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    sentinel_inode = sentinel.stat().st_ino
    (build_dir / "removed.ts").write_text("stale", encoding="utf-8")
    (build_dir / "dist").mkdir()
    (build_dir / "dist" / "index.html").write_text("stale", encoding="utf-8")
    monkeypatch.setattr(dashboard_build.shutil, "which", lambda _name: "/fake/pnpm")
    commands: list[tuple[str, list[str]]] = []

    def run_step(result, name, command, **_kwargs):
        commands.append((name, command))
        if name == "pnpm_build":
            dist = build_dir / "dist"
            dist.mkdir()
            (dist / "index.html").write_text("target dashboard", encoding="utf-8")
        result.add_step(name, True, duration_sec=0)

    monkeypatch.setattr(dashboard_build, "run_step", run_step)
    result = _result(monkeypatch)

    dashboard_build.prepare_dashboard(
        result,
        source=source,
        release_root=release_root,
        release=target,
        commit=target_commit,
        active_release=active,
        active_commit=active_commit,
    )

    assert commands == [
        (
            "pnpm_install",
            [
                "/fake/pnpm",
                "--dir",
                str(build_dir),
                "install",
                "--frozen-lockfile",
                "--prefer-offline",
            ],
        ),
        ("pnpm_build", ["/fake/pnpm", "--dir", str(build_dir), "build"]),
    ]
    assert sentinel.is_file()
    assert sentinel.stat().st_ino == sentinel_inode
    assert not (build_dir / "removed.ts").exists()
    assert (build_dir / "app.ts").read_text(encoding="utf-8").endswith("2\n")
    assert (target / "dashboard" / "dist" / "index.html").read_text(
        encoding="utf-8"
    ) == "target dashboard"
    assert result.steps[-1]["detail"] == f"built in persistent dir {build_dir}"


def test_failed_candidate_cleanup_preserves_persistent_dashboard_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atomic_release = _import_script(monkeypatch, "haniel_atomic_release")
    dashboard_build = _import_script(monkeypatch, "haniel_dashboard_build")
    source, active_commit, target_commit = _source_repo(
        tmp_path, dashboard_changed=True
    )
    release_root = tmp_path / "release-root"
    releases = release_root / "releases"
    active = releases / active_commit[:12]
    (active / "dashboard" / "dist").mkdir(parents=True)
    (active / "dashboard" / "dist" / "index.html").write_text(
        "active dashboard", encoding="utf-8"
    )
    persistent = release_root / "dashboard-build"
    (persistent / "node_modules").mkdir(parents=True)
    sentinel = persistent / "node_modules" / ".persistent-sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    bootstrap_python = tmp_path / "python"
    bootstrap_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(atomic_release, "_require_disk_space", lambda *_args: None)
    monkeypatch.setattr(dashboard_build.shutil, "which", lambda _name: "/fake/pnpm")

    def release_step(result, name, command, **_kwargs):
        if name == "release_checkout":
            shutil.copytree(source, Path(command[-1]))
        elif name == "venv_create":
            python = Path(command[-1]) / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
        result.add_step(name, True, duration_sec=0)

    def fail_build(result, name, command, **_kwargs):
        if name == "pnpm_build":
            result.add_step(name, False, "injected build failure", duration_sec=0)
            raise atomic_release.ReleasePreparationError(result.error)
        result.add_step(name, True, duration_sec=0)

    monkeypatch.setattr(atomic_release, "_run_step", release_step)
    monkeypatch.setattr(dashboard_build, "run_step", fail_build)
    result = atomic_release.PreparationResult()

    with pytest.raises(atomic_release.ReleasePreparationError):
        atomic_release._prepare_release(
            result,
            source=source,
            releases=releases,
            commit=target_commit,
            bootstrap_python=bootstrap_python,
            min_free_mb=1,
            active_release=active,
            active_commit=active_commit,
        )

    assert not (releases / target_commit[:12]).exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_release_prune_preserves_persistent_dashboard_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _import_script(monkeypatch, "haniel_release_policy")
    release_root = tmp_path / "release-root"
    releases = release_root / "releases"
    current = releases / ("a" * 40)
    previous = releases / ("b" * 40)
    stale = releases / ("c" * 40)
    for release in (current, previous, stale):
        release.mkdir(parents=True)
        (release / policy.READY_MARKER).write_text(
            json.dumps({"version": 1, "commit": release.name}), encoding="utf-8"
        )
    (release_root / "current.txt").write_text(f"{current.name}\n", encoding="utf-8")
    persistent = release_root / "dashboard-build" / "node_modules"
    persistent.mkdir(parents=True)
    sentinel = persistent / ".persistent-sentinel"
    sentinel.write_text("keep", encoding="utf-8")

    outcome = policy.prune_ready_releases(
        release_root,
        current=current,
        previous=previous,
        retain_extra=0,
    )

    assert outcome.deleted == (stale.name,)
    assert not stale.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"
