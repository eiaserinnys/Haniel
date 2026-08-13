"""Tests for the orchestrator dashboard static-file boundary."""

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import pytest
from starlette.testclient import TestClient

from haniel_orch.server import OrchestratorConfig, OrchestratorServer


def build_dashboard_client(dashboard_dir: Path) -> TestClient:
    """Build an app with an isolated dashboard directory."""
    app = OrchestratorServer(
        OrchestratorConfig(token="node-secret", dashboard_dir=str(dashboard_dir))
    ).build_app()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def dashboard(tmp_path: Path) -> Iterator[tuple[TestClient, Path, Path]]:
    dashboard_dir = tmp_path / "dashboard"
    dashboard_dir.mkdir()
    (dashboard_dir / "index.html").write_text("dashboard index", encoding="utf-8")
    (dashboard_dir / "app.js").write_text("dashboard asset", encoding="utf-8")
    secret_path = tmp_path / "secret.txt"
    secret_path.write_text("outside secret", encoding="utf-8")
    client = build_dashboard_client(dashboard_dir)
    yield client, dashboard_dir, secret_path
    client.close()


@pytest.mark.parametrize(
    "traversal",
    [
        "%2e%2e/secret.txt",
        "%2E%2E/secret.txt",
        "%2e%2e%2fsecret.txt",
    ],
)
def test_dashboard_rejects_single_encoded_traversal(
    dashboard: tuple[TestClient, Path, Path], traversal: str
) -> None:
    client, _, _ = dashboard

    response = client.get(f"/dashboard/{traversal}")

    assert response.status_code == 404


@pytest.mark.parametrize(
    "traversal",
    [
        "../secret.txt",
        "%252e%252e/secret.txt",
        "..%5Csecret.txt",
    ],
)
def test_dashboard_rejects_noncanonical_paths(
    dashboard: tuple[TestClient, Path, Path], traversal: str
) -> None:
    client, _, _ = dashboard

    response = client.get(f"/dashboard/{traversal}")

    assert response.status_code == 404


def test_dashboard_rejects_absolute_path(
    dashboard: tuple[TestClient, Path, Path],
) -> None:
    client, _, secret_path = dashboard
    encoded_absolute_path = quote(str(secret_path), safe="")

    response = client.get(f"/dashboard/{encoded_absolute_path}")

    assert response.status_code == 404


def test_dashboard_rejects_symlink_escape(
    dashboard: tuple[TestClient, Path, Path],
) -> None:
    client, dashboard_dir, secret_path = dashboard
    (dashboard_dir / "outside-link").symlink_to(secret_path)

    response = client.get("/dashboard/outside-link")

    assert response.status_code == 404


def test_dashboard_serves_normal_asset(
    dashboard: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = dashboard

    response = client.get("/dashboard/app.js")

    assert response.status_code == 200
    assert response.text == "dashboard asset"


@pytest.mark.parametrize("path", ["/dashboard", "/dashboard/settings/profile"])
def test_dashboard_keeps_spa_fallback(
    dashboard: tuple[TestClient, Path, Path], path: str
) -> None:
    client, _, _ = dashboard

    response = client.get(path)

    assert response.status_code == 200
    assert response.text == "dashboard index"
