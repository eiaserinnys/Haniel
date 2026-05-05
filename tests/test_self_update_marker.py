"""Tests for self_update_marker module."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from haniel.core.self_update_marker import (
    MARKER_RELPATH,
    SCHEMA_VERSION,
    SelfUpdateResult,
    SelfUpdateStep,
    read_and_consume,
    write,
)


def _make_result(ok: bool = True) -> SelfUpdateResult:
    return SelfUpdateResult(
        version=SCHEMA_VERSION,
        started_at="2026-05-05T12:00:00.000+09:00",
        finished_at="2026-05-05T12:01:30.000+09:00",
        ok=ok,
        steps=[
            SelfUpdateStep(name="git_fetch", ok=True),
            SelfUpdateStep(
                name="git_reset",
                ok=ok,
                error=None if ok else "fatal: bad ref",
            ),
        ],
        error=None if ok else "git_reset failed: fatal: bad ref",
    )


def test_read_and_consume_returns_none_when_missing(tmp_path: Path) -> None:
    """No marker file present → returns None, no error."""
    result = read_and_consume(tmp_path)
    assert result is None
    # Also verify nothing was created
    assert not (tmp_path / MARKER_RELPATH).exists()


def test_read_and_consume_parses_valid_marker_and_deletes_file(tmp_path: Path) -> None:
    """Valid marker is parsed, returned, and the file is deleted."""
    expected = _make_result(ok=False)
    write(tmp_path, expected)
    marker = tmp_path / MARKER_RELPATH
    assert marker.exists()  # precondition

    actual = read_and_consume(tmp_path)
    assert actual is not None
    assert actual.version == SCHEMA_VERSION
    assert actual.ok is False
    assert actual.error == "git_reset failed: fatal: bad ref"
    assert len(actual.steps) == 2
    assert actual.steps[0].name == "git_fetch"
    assert actual.steps[1].ok is False
    # Marker is consumed (deleted)
    assert not marker.exists()


def test_read_and_consume_returns_none_on_malformed_json_and_deletes_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Broken JSON → returns None, deletes file, logs warning."""
    marker = tmp_path / MARKER_RELPATH
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="haniel.core.self_update_marker"):
        actual = read_and_consume(tmp_path)

    assert actual is None
    assert not marker.exists()
    assert any("malformed" in rec.getMessage() for rec in caplog.records)


def test_read_and_consume_returns_none_on_unsupported_version(tmp_path: Path) -> None:
    """Marker with unsupported version → returns None, deletes file."""
    marker = tmp_path / MARKER_RELPATH
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "version": 2,
                "started_at": "2026-05-05T00:00:00+00:00",
                "finished_at": "2026-05-05T00:01:00+00:00",
                "ok": True,
                "steps": [],
                "error": None,
            }
        ),
        encoding="utf-8",
    )

    actual = read_and_consume(tmp_path)
    assert actual is None
    assert not marker.exists()


def test_write_creates_local_dir_if_missing(tmp_path: Path) -> None:
    """write() creates the .local/ directory if absent."""
    assert not (tmp_path / ".local").exists()
    write(tmp_path, _make_result(ok=True))
    assert (tmp_path / ".local").is_dir()
    assert (tmp_path / MARKER_RELPATH).is_file()


def test_round_trip_write_then_read_and_consume(tmp_path: Path) -> None:
    """Round-trip: write a result, read it back via consume."""
    expected = _make_result(ok=True)
    write(tmp_path, expected)

    actual = read_and_consume(tmp_path)
    assert actual is not None
    assert actual.to_dict() == expected.to_dict()
    # File consumed
    assert not (tmp_path / MARKER_RELPATH).exists()
