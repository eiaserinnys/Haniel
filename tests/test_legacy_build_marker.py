"""Tests for the legacy post_pull build marker."""

import json
import os
from pathlib import Path

from haniel.core import legacy_build_marker


def test_missing_marker_reads_as_empty(tmp_path: Path) -> None:
    assert legacy_build_marker.read_completed_commits(tmp_path) == {}


def test_written_commits_round_trip(tmp_path: Path) -> None:
    committed = {"app-repo": "a" * 40, "tool-repo": "b" * 40}

    legacy_build_marker.write_completed_commits(tmp_path, committed)

    path = legacy_build_marker.marker_path(tmp_path)
    assert path == tmp_path / ".haniel" / "legacy_post_pull.json"
    assert json.loads(path.read_text(encoding="utf-8")) == committed
    assert legacy_build_marker.read_completed_commits(tmp_path) == committed


def test_read_ignores_corrupt_marker(tmp_path: Path) -> None:
    path = legacy_build_marker.marker_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    assert legacy_build_marker.read_completed_commits(tmp_path) == {}


def test_read_ignores_non_mapping_entries(tmp_path: Path) -> None:
    path = legacy_build_marker.marker_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"app-repo": 7, "good": "abc"}), encoding="utf-8")

    assert legacy_build_marker.read_completed_commits(tmp_path) == {"good": "abc"}


def test_sync_disk_flushes_through_os_sync(monkeypatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(os, "sync", lambda: calls.append(True))

    legacy_build_marker.sync_disk()

    assert calls == [True]


def test_sync_disk_is_skipped_without_os_sync(monkeypatch) -> None:
    monkeypatch.delattr(os, "sync", raising=False)

    legacy_build_marker.sync_disk()
