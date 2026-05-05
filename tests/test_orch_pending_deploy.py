"""Tests for orch_pending_deploy marker module."""
from __future__ import annotations

import json
from pathlib import Path

from haniel.core.orch_pending_deploy import (
    MARKER_RELPATH,
    SCHEMA_VERSION,
    OrchPendingDeploy,
    read_and_consume,
    write,
)


class TestWriteAndRead:
    def test_write_creates_file(self, tmp_path: Path) -> None:
        write(tmp_path, "node:repo:main:abc1234", "2026-05-05T00:00:00+00:00")
        assert (tmp_path / MARKER_RELPATH).exists()

    def test_read_returns_parsed(self, tmp_path: Path) -> None:
        write(tmp_path, "node:repo:main:abc1234", "2026-05-05T00:00:00+00:00")
        result = read_and_consume(tmp_path)
        assert result is not None
        assert result.deploy_id == "node:repo:main:abc1234"
        assert result.started_at == "2026-05-05T00:00:00+00:00"
        assert result.version == SCHEMA_VERSION

    def test_read_consumes_file(self, tmp_path: Path) -> None:
        write(tmp_path, "a:b:c:d", "t")
        read_and_consume(tmp_path)
        assert not (tmp_path / MARKER_RELPATH).exists()

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        assert read_and_consume(tmp_path) is None

    def test_dataclass_to_dict_roundtrip(self) -> None:
        item = OrchPendingDeploy(version=1, deploy_id="x", started_at="t")
        assert item.to_dict() == {
            "version": 1, "deploy_id": "x", "started_at": "t",
        }


class TestMalformedHandling:
    def test_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / MARKER_RELPATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json")
        assert read_and_consume(tmp_path) is None
        # Always consumed even on parse error so we don't loop on bad files
        assert not path.exists()

    def test_wrong_version(self, tmp_path: Path) -> None:
        path = tmp_path / MARKER_RELPATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"version": 99, "deploy_id": "x", "started_at": "t"}
            )
        )
        assert read_and_consume(tmp_path) is None
        assert not path.exists()

    def test_not_object(self, tmp_path: Path) -> None:
        path = tmp_path / MARKER_RELPATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([1, 2, 3]))
        assert read_and_consume(tmp_path) is None
        assert not path.exists()
