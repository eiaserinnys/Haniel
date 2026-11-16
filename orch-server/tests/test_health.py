"""Unauthenticated health response exposes only the running revision."""

import subprocess
from types import SimpleNamespace

from starlette.testclient import TestClient

from haniel_orch.server import OrchestratorConfig, OrchestratorServer, create_app


def test_health_is_public_and_has_only_the_expected_fields(tmp_path):
    app = OrchestratorServer(OrchestratorConfig(
        token="node-secret", auth_bearer_token="dashboard-secret",
        db_path=str(tmp_path / "test.db"), source_commit="abc",
    )).build_app()
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "haniel-orchestrator", "source_commit": "abc"}


def test_health_reports_null_when_git_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN", "node-secret")
    monkeypatch.setenv("AUTH_BEARER_TOKEN", "dashboard-secret")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    def fail(*args, **kwargs):
        raise FileNotFoundError("git")
    monkeypatch.setattr(subprocess, "run", fail)
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert response.json()["source_commit"] is None


def test_create_app_reads_revision_once_at_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN", "node-secret")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    calls = []

    def git(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="a" * 40 + "\n")

    monkeypatch.setattr(subprocess, "run", git)
    client = TestClient(create_app())
    assert client.get("/health").json()["source_commit"] == "a" * 40
    assert client.get("/health").json()["source_commit"] == "a" * 40
    assert len(calls) == 1
    assert calls[0][0][0][-2:] == ["rev-parse", "HEAD"]
    assert calls[0][1]["timeout"] > 0
