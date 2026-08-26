"""Contracts for atomic release step timing and deferred pruning."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _atomic_release(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    return importlib.import_module("haniel_atomic_release")


def _ready_release(release_root: Path, commit: str) -> Path:
    release = release_root / "releases" / commit
    release.mkdir(parents=True)
    (release / ".haniel-release-ready.json").write_text(
        json.dumps({"version": 1, "commit": commit}),
        encoding="utf-8",
    )
    return release


def _release_python(release: Path) -> Path:
    if os.name == "nt":
        return release / ".venv" / "Scripts" / "python.exe"
    return release / ".venv" / "bin" / "python"


def test_step_recorder_persists_duration_and_emits_canonical_log(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    atomic_release = _atomic_release(monkeypatch)
    result = atomic_release.PreparationResult()

    result.add_step("git_fetch", True, duration_sec=1.25)

    assert result.steps == [
        {"name": "git_fetch", "ok": True, "error": None, "duration_sec": 1.25}
    ]
    assert capsys.readouterr().err.strip() == (
        "[haniel-release] step=git_fetch status=ok duration_sec=1.250"
    )


def test_prune_only_consumes_durable_request_and_preserves_previous(
    tmp_path: Path,
) -> None:
    release_root = tmp_path / "releases-root"
    current_commit = "a" * 40
    previous_commit = "b" * 40
    stale_commit = "c" * 40
    _ready_release(release_root, current_commit)
    _ready_release(release_root, previous_commit)
    stale = _ready_release(release_root, stale_commit)
    (release_root / "current.txt").write_text(f"{current_commit}\n", encoding="utf-8")
    request = release_root / ".haniel-release-prune-request.json"
    request.write_text(
        json.dumps(
            {
                "version": 1,
                "current": current_commit,
                "previous": previous_commit,
                "retain_extra": 0,
            }
        ),
        encoding="utf-8",
    )
    result_path = tmp_path / "haniel_release_prune.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "haniel_atomic_release.py"),
            "--prune-only",
            "--release-root",
            str(release_root),
            "--result-json",
            str(result_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["warnings"] == []
    assert payload["steps"][-1]["name"] == "release_prune"
    assert payload["steps"][-1]["duration_sec"] >= 0
    assert not request.exists()
    assert not stale.exists()
    assert (release_root / "releases" / current_commit).exists()
    assert (release_root / "releases" / previous_commit).exists()


def test_prune_failure_is_warning_and_does_not_fail_launch_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    prune_module = importlib.import_module("haniel_release_prune")
    release_root = tmp_path / "releases-root"
    current_commit = "d" * 40
    previous_commit = "e" * 40
    _ready_release(release_root, current_commit)
    _ready_release(release_root, previous_commit)
    (release_root / "current.txt").write_text(f"{current_commit}\n", encoding="utf-8")
    request = release_root / ".haniel-release-prune-request.json"
    request.write_text(
        json.dumps(
            {
                "version": 1,
                "current": current_commit,
                "previous": previous_commit,
                "retain_extra": 0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        prune_module,
        "prune_ready_releases",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("locked")),
    )

    result = prune_module.prune(str(release_root))

    assert result.ok is True
    assert result.warnings == ["release cleanup failed: locked"]
    assert result.steps[-1]["name"] == "release_prune"
    assert result.steps[-1]["ok"] is False
    assert not request.exists()


def test_prune_invalidates_ready_marker_before_recursive_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    inventory = importlib.import_module("haniel_release_inventory")
    policy = importlib.import_module("haniel_release_policy")
    release_root = tmp_path / "releases-root"
    current = _ready_release(release_root, "1" * 40)
    previous = _ready_release(release_root, "2" * 40)
    candidate = _ready_release(release_root, "3" * 40)
    python = _release_python(candidate)
    python.parent.mkdir(parents=True)
    python.write_text("partial", encoding="utf-8")
    (release_root / "current.txt").write_text(f"{current.name}\n", encoding="utf-8")
    monkeypatch.setattr(
        policy,
        "remove_tree",
        lambda _path: (_ for _ in ()).throw(OSError("interrupted")),
    )

    outcome = policy.prune_ready_releases(
        release_root,
        current=current,
        previous=previous,
        retain_extra=0,
    )

    assert outcome.failures == (f"{candidate.name}: interrupted",)
    assert policy.read_ready_commit(candidate) is None
    assert (
        inventory.ready_release_for_commit(release_root / "releases", "3" * 40) is None
    )
