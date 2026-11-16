"""Unauthenticated health response exposes only the running revision."""

import subprocess
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import haniel_orch.server as server
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
    monkeypatch.setattr(server, "__file__", str(tmp_path / "src/haniel_orch/server.py"))
    def fail(*args, **kwargs):
        raise FileNotFoundError("git")
    monkeypatch.setattr(subprocess, "run", fail)
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert response.json()["source_commit"] is None


def test_create_app_reads_revision_once_at_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN", "node-secret")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(server, "__file__", str(tmp_path / "src/haniel_orch/server.py"))
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
    assert calls[0][1]["timeout"] == 5


def test_release_sha_takes_precedence_and_is_read_once(monkeypatch, tmp_path):
    release_sha = tmp_path / ".release-sha"
    release_sha.write_text("b" * 40 + "\n")
    monkeypatch.setenv("TOKEN", "node-secret")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(server, "__file__", str(tmp_path / "src/haniel_orch/server.py"))

    def unexpected_git(*args, **kwargs):
        pytest.fail("git should not be called when the release SHA is valid")

    monkeypatch.setattr(subprocess, "run", unexpected_git)
    client = TestClient(create_app())
    assert client.get("/health").json()["source_commit"] == "b" * 40
    release_sha.write_text("c" * 40)
    assert client.get("/health").json()["source_commit"] == "b" * 40


@pytest.mark.parametrize("invalid_sha", ["abc", "A" * 40, ""])
def test_invalid_release_sha_warns_and_falls_back_to_git(monkeypatch, tmp_path, caplog, invalid_sha):
    release_sha = tmp_path / ".release-sha"
    release_sha.write_text(invalid_sha)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="d" * 40 + "\n"))

    assert server._read_source_commit(tmp_path) == "d" * 40
    assert len(caplog.records) == 1
    assert str(release_sha) in caplog.text
    if invalid_sha:
        assert invalid_sha not in caplog.text


def test_unreadable_release_sha_falls_back_to_git(monkeypatch, tmp_path):
    def fail_read(*args, **kwargs):
        raise OSError("cannot read")

    monkeypatch.setattr(server.pathlib.Path, "read_text", fail_read)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="e" * 40))
    assert server._read_source_commit(tmp_path) == "e" * 40
