"""Tests for orch_pending_deploy marker module."""

from __future__ import annotations

import json
from pathlib import Path

from haniel.core.orch_pending_deploy import (
    MARKER_RELPATH,
    SCHEMA_VERSION,
    OrchPendingDeploy,
    discard,
    read_and_consume,
    write,
)


class TestWriteAndRead:
    @staticmethod
    def _write(tmp_path: Path, deploy_id: str, started_at: str) -> None:
        write(
            tmp_path,
            deploy_id,
            started_at,
            orchestrator_attempt_id="orch-1",
            connection_generation="generation-1",
            execution_mode="execute",
            probe_id="probe-1",
            preflight_fingerprint="fingerprint-1",
        )

    def test_write_creates_file(self, tmp_path: Path) -> None:
        self._write(tmp_path, "node:repo:main:abc1234", "2026-05-05T00:00:00+00:00")
        assert (tmp_path / MARKER_RELPATH).exists()

    def test_read_returns_parsed(self, tmp_path: Path) -> None:
        self._write(tmp_path, "node:repo:main:abc1234", "2026-05-05T00:00:00+00:00")
        result = read_and_consume(tmp_path)
        assert result is not None
        assert result.deploy_id == "node:repo:main:abc1234"
        assert result.started_at == "2026-05-05T00:00:00+00:00"
        assert result.version == SCHEMA_VERSION
        assert result.orchestrator_attempt_id == "orch-1"
        assert result.connection_generation == "generation-1"

    def test_read_consumes_file(self, tmp_path: Path) -> None:
        self._write(tmp_path, "a:b:c:d", "t")
        read_and_consume(tmp_path)
        assert not (tmp_path / MARKER_RELPATH).exists()

    def test_discard_only_removes_matching_deploy(self, tmp_path: Path) -> None:
        self._write(tmp_path, "node:repo:main:approved", "t")
        assert discard(tmp_path, expected_deploy_id="different") is False
        assert (tmp_path / MARKER_RELPATH).exists()
        assert discard(tmp_path, expected_deploy_id="node:repo:main:approved") is True
        assert not (tmp_path / MARKER_RELPATH).exists()

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        assert read_and_consume(tmp_path) is None

    def test_dataclass_to_dict_roundtrip(self) -> None:
        item = OrchPendingDeploy(
            version=SCHEMA_VERSION,
            deploy_id="x",
            started_at="t",
            orchestrator_attempt_id="orch-1",
            connection_generation="generation-1",
            execution_mode="evidence_recovery",
            probe_id="probe-1",
            preflight_fingerprint="fingerprint-1",
        )
        assert item.to_dict() == {
            "version": SCHEMA_VERSION,
            "deploy_id": "x",
            "started_at": "t",
            "orchestrator_attempt_id": "orch-1",
            "connection_generation": "generation-1",
            "execution_mode": "evidence_recovery",
            "probe_id": "probe-1",
            "preflight_fingerprint": "fingerprint-1",
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
            json.dumps({"version": 99, "deploy_id": "x", "started_at": "t"})
        )
        assert read_and_consume(tmp_path) is None
        assert not path.exists()

    def test_not_object(self, tmp_path: Path) -> None:
        path = tmp_path / MARKER_RELPATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([1, 2, 3]))
        assert read_and_consume(tmp_path) is None
        assert not path.exists()
