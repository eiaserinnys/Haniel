"""Cross-platform contracts for Haniel release pointers and cleanup."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _import_script(monkeypatch: pytest.MonkeyPatch, name: str):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    return importlib.import_module(name)


def _ready_release(release_root: Path, commit: str) -> Path:
    release = release_root / "releases" / commit
    python = release / (
        ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
    )
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    (release / ".haniel-release-ready.json").write_text(
        json.dumps({"version": 1, "commit": commit}),
        encoding="utf-8",
    )
    return release


def test_active_release_migrates_legacy_symlink_to_pointer_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atomic_release = _import_script(monkeypatch, "haniel_atomic_release")
    release_root = tmp_path / "releases-root"
    release = _ready_release(release_root, "a" * 40)
    legacy = release_root / "current"
    legacy.symlink_to(Path("releases") / release.name, target_is_directory=True)

    assert atomic_release._active_release(release_root) == release.resolve()
    assert (release_root / "current.txt").read_text(encoding="utf-8") == (
        f"{release.name}\n"
    )
    assert not legacy.exists()
    assert not legacy.is_symlink()


def test_switch_uses_atomic_file_replace_not_directory_reparse_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atomic_release = _import_script(monkeypatch, "haniel_atomic_release")
    release_fs = _import_script(monkeypatch, "haniel_release_fs")
    release_root = tmp_path / "releases-root"
    first = _ready_release(release_root, "a" * 40)
    second = _ready_release(release_root, "b" * 40)
    (release_root / "current").symlink_to(
        Path("releases") / first.name,
        target_is_directory=True,
    )
    real_replace = release_fs.os.replace

    def windows_replace(source, destination):
        if Path(destination).name == "current":
            error = PermissionError("directory reparse points cannot be replaced")
            error.winerror = 5
            raise error
        return real_replace(source, destination)

    monkeypatch.setattr(release_fs.os, "replace", windows_replace)
    result = atomic_release.PreparationResult()

    atomic_release._switch_current(result, release_root, second)

    assert result.switched is True
    assert (release_root / "current.txt").read_text(encoding="utf-8") == (
        f"{second.name}\n"
    )


def test_remove_tree_handles_paths_over_260_characters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_fs = _import_script(monkeypatch, "haniel_release_fs")
    root = tmp_path / "stale-release"
    deep = root
    while len(str(deep / "payload.txt")) <= 280:
        deep /= "segment-padding"
    deep.mkdir(parents=True)
    (deep / "payload.txt").write_text("stale", encoding="utf-8")

    release_fs.remove_tree(root)

    assert not root.exists()


def test_remove_tree_treats_directory_symlink_as_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_fs = _import_script(monkeypatch, "haniel_release_fs")
    target = tmp_path / "target"
    target.mkdir()
    payload = target / "payload.txt"
    payload.write_text("preserve", encoding="utf-8")
    root = tmp_path / "stale-release"
    root.mkdir()
    (root / "junction-like-link").symlink_to(target, target_is_directory=True)

    release_fs.remove_tree(root)

    assert payload.read_text(encoding="utf-8") == "preserve"


def test_windows_paths_receive_extended_length_prefix_after_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_fs = _import_script(monkeypatch, "haniel_release_fs")

    assert release_fs.normalized_os_path(
        r"D:\haniel-root\releases\candidate",
        platform="nt",
    ) == r"\\?\D:\haniel-root\releases\candidate"
    assert release_fs.normalized_os_path(
        r"\\server\share\candidate",
        platform="nt",
    ) == r"\\?\UNC\server\share\candidate"


def test_broken_candidate_uses_a_distinct_retry_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atomic_release = _import_script(monkeypatch, "haniel_atomic_release")
    releases = tmp_path / "release-root" / "releases"
    commit = "c" * 40
    poisoned = releases / commit[:12]
    poisoned.mkdir(parents=True)
    (poisoned / atomic_release.BROKEN_MARKER).write_text("{}", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    bootstrap_python = tmp_path / "python"
    bootstrap_python.write_text("", encoding="utf-8")
    result = atomic_release.PreparationResult()

    monkeypatch.setattr(atomic_release, "_require_disk_space", lambda *_args: None)

    def run_step(step_result, name, command, **_kwargs):
        if name == "release_checkout":
            Path(command[-1]).mkdir(parents=True)
        elif name == "venv_create":
            candidate_python = Path(command[-1]) / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            candidate_python.parent.mkdir(parents=True)
            candidate_python.write_text("", encoding="utf-8")
        step_result.add_step(name, True)

    monkeypatch.setattr(atomic_release, "_run_step", run_step)

    candidate = atomic_release._prepare_release(
        result,
        source=source,
        releases=releases,
        commit=commit,
        bootstrap_python=bootstrap_python,
        min_free_mb=1,
    )

    assert candidate != poisoned
    assert candidate.name.startswith(f"{commit[:12]}.retry-")
    assert poisoned.is_dir()
    assert atomic_release.read_ready_commit(candidate) == commit


def test_failed_cleanup_records_broken_marker_for_next_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atomic_release = _import_script(monkeypatch, "haniel_atomic_release")
    releases = tmp_path / "release-root" / "releases"
    source = tmp_path / "source"
    source.mkdir()
    bootstrap_python = tmp_path / "python"
    bootstrap_python.write_text("", encoding="utf-8")
    commit = "d" * 40
    result = atomic_release.PreparationResult()

    monkeypatch.setattr(atomic_release, "_require_disk_space", lambda *_args: None)

    def fail_after_checkout(step_result, name, command, **_kwargs):
        if name == "release_checkout":
            Path(command[-1]).mkdir(parents=True)
            step_result.add_step(name, True)
            return
        step_result.add_step(name, False, "injected build failure")
        raise atomic_release.ReleasePreparationError(step_result.error)

    monkeypatch.setattr(atomic_release, "_run_step", fail_after_checkout)
    monkeypatch.setattr(
        atomic_release,
        "remove_tree",
        lambda _path: (_ for _ in ()).throw(OSError("injected cleanup failure")),
    )

    with pytest.raises(atomic_release.ReleasePreparationError):
        atomic_release._prepare_release(
            result,
            source=source,
            releases=releases,
            commit=commit,
            bootstrap_python=bootstrap_python,
            min_free_mb=1,
        )

    marker = releases / commit[:12] / atomic_release.BROKEN_MARKER
    assert marker.is_file()
    assert "injected cleanup failure" in marker.read_text(encoding="utf-8")


def test_short_sha_collision_preserves_existing_ready_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atomic_release = _import_script(monkeypatch, "haniel_atomic_release")
    releases = tmp_path / "release-root" / "releases"
    first_commit = "a" * 12 + "b" * 28
    second_commit = "a" * 12 + "c" * 28
    existing = releases / first_commit[:12]
    python = existing / (
        ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
    )
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    (existing / ".haniel-release-ready.json").write_text(
        json.dumps({"version": 1, "commit": first_commit}),
        encoding="utf-8",
    )

    candidate = atomic_release._select_release_path(releases, second_commit)

    assert candidate != existing
    assert candidate.name.startswith(f"{second_commit[:12]}.retry-")
    assert atomic_release.read_ready_commit(existing) == first_commit
