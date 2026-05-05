"""Tests for authentication — AuthMiddleware, hub WS auth, auth module."""

import hmac
import hashlib
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket

from haniel_orch.auth import AuthConfig, SESSION_COOKIE, SESSION_MAX_AGE
from haniel_orch.hub import WebSocketHub
from haniel_orch.event_store import EventStore
from haniel_orch.node_registry import NodeRegistry
from haniel_orch.server import AuthMiddleware


# ── AuthMiddleware Tests ──────────────────────────────────────────────


class TestAuthMiddleware:
    """AuthMiddleware guards /api/* with Bearer token."""

    def _make_app(self, auth_token: str = "secret-token") -> Starlette:
        """Create a minimal Starlette app wrapped with AuthMiddleware."""

        async def api_endpoint(request: Request) -> JSONResponse:
            return JSONResponse({"ok": True})

        async def non_api_endpoint(request: Request) -> JSONResponse:
            return JSONResponse({"public": True})

        app = Starlette(routes=[
            Route("/api/orch/test", api_endpoint),
            Route("/auth/login", non_api_endpoint),
            Route("/dashboard", non_api_endpoint),
            Route("/other", non_api_endpoint),
        ])
        return AuthMiddleware(app, auth_bearer_token=auth_token)

    def test_api_request_without_token_returns_401(self):
        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/orch/test")
        assert resp.status_code == 401
        assert resp.json()["error"] == "unauthorized"

    def test_api_request_with_wrong_token_returns_401(self):
        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/api/orch/test",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_api_request_with_correct_token_passes(self):
        app = self._make_app("my-secret")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/api/orch/test",
            headers={"Authorization": "Bearer my-secret"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_non_api_routes_pass_without_token(self):
        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/auth/login").status_code == 200
        assert client.get("/dashboard").status_code == 200
        assert client.get("/other").status_code == 200

    def test_empty_auth_token_disables_auth(self):
        """When auth_bearer_token is empty, all requests pass through."""

        async def api_endpoint(request: Request) -> JSONResponse:
            return JSONResponse({"ok": True})

        app = Starlette(routes=[Route("/api/orch/test", api_endpoint)])
        wrapped = AuthMiddleware(app, auth_bearer_token="")
        client = TestClient(wrapped, raise_server_exceptions=False)
        resp = client.get("/api/orch/test")
        assert resp.status_code == 200


# ── Hub Dashboard WS Auth Tests ───────────────────────────────────────


class TestHubDashboardWsAuth:
    """WebSocket dashboard authentication via query param token."""

    @pytest.fixture
    async def store(self):
        s = EventStore(":memory:")
        await s.initialize()
        yield s
        await s.close()

    @pytest.fixture
    async def hub_with_auth(self, store):
        registry = NodeRegistry(store)
        return WebSocketHub(
            registry, store, token="node-token", auth_bearer_token="dash-secret"
        )

    @pytest.fixture
    async def hub_no_auth(self, store):
        registry = NodeRegistry(store)
        return WebSocketHub(registry, store, token="node-token", auth_bearer_token="")

    def test_ws_with_valid_token_connects(self, hub_with_auth):
        """Dashboard WS with correct ?token= should be accepted."""

        async def ws_endpoint(websocket: WebSocket):
            await hub_with_auth.handle_dashboard_ws(websocket)

        app = Starlette(routes=[WebSocketRoute("/ws/dashboard", ws_endpoint)])
        client = TestClient(app)

        with client.websocket_connect("/ws/dashboard?token=dash-secret") as ws:
            # Connection accepted — we're connected
            assert ws is not None

    def test_ws_without_token_rejected(self, hub_with_auth):
        """Dashboard WS without token should be closed with 4001."""

        async def ws_endpoint(websocket: WebSocket):
            await hub_with_auth.handle_dashboard_ws(websocket)

        app = Starlette(routes=[WebSocketRoute("/ws/dashboard", ws_endpoint)])
        client = TestClient(app)

        # WebSocket close before accept → raises exception
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/dashboard"):
                pass

    def test_ws_with_wrong_token_rejected(self, hub_with_auth):
        """Dashboard WS with wrong token should be closed with 4001."""

        async def ws_endpoint(websocket: WebSocket):
            await hub_with_auth.handle_dashboard_ws(websocket)

        app = Starlette(routes=[WebSocketRoute("/ws/dashboard", ws_endpoint)])
        client = TestClient(app)

        with pytest.raises(Exception):
            with client.websocket_connect("/ws/dashboard?token=wrong"):
                pass

    def test_ws_no_auth_connects_without_token(self, hub_no_auth):
        """When auth_bearer_token is empty, WS connects without token."""

        async def ws_endpoint(websocket: WebSocket):
            await hub_no_auth.handle_dashboard_ws(websocket)

        app = Starlette(routes=[WebSocketRoute("/ws/dashboard", ws_endpoint)])
        client = TestClient(app)

        with client.websocket_connect("/ws/dashboard") as ws:
            assert ws is not None


# ── AuthConfig Token Tests ────────────────────────────────────────────


class TestAuthConfigTokens:
    """Session token creation and verification."""

    @pytest.fixture
    def config(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
        monkeypatch.setenv("ALLOWED_EMAIL", "user@example.com")
        monkeypatch.setenv("AUTH_BEARER_TOKEN", "test-bearer")
        monkeypatch.setenv("SESSION_SECRET", "test-secret-key")
        monkeypatch.setenv("BASE_URL", "http://localhost:9300")
        return AuthConfig()

    def test_create_and_verify_session_token(self, config):
        token = config.create_session_token("user@example.com")
        email = config.verify_session_token(token)
        assert email == "user@example.com"

    def test_verify_invalid_signature_returns_none(self, config):
        token = config.create_session_token("user@example.com")
        # Tamper with signature
        parts = token.rsplit(".", 1)
        tampered = parts[0] + ".invalid_signature"
        assert config.verify_session_token(tampered) is None

    def test_verify_expired_token_returns_none(self, config):
        # Create token with already-expired time
        import base64
        payload = json.dumps({"email": "user@example.com", "exp": int(time.time()) - 100})
        sig = hmac.new(
            config.session_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        token = base64.urlsafe_b64encode(payload.encode()).decode() + "." + sig
        assert config.verify_session_token(token) is None

    def test_verify_bearer_correct(self, config):
        request = AsyncMock()
        request.headers = {"authorization": "Bearer test-bearer"}
        assert config.verify_bearer(request) is True

    def test_verify_bearer_wrong(self, config):
        request = AsyncMock()
        request.headers = {"authorization": "Bearer wrong"}
        assert config.verify_bearer(request) is False

    def test_verify_bearer_missing(self, config):
        request = AsyncMock()
        request.headers = {}
        assert config.verify_bearer(request) is False

    def test_verify_ws_token_correct(self, config):
        assert config.verify_ws_token("test-bearer") is True

    def test_verify_ws_token_wrong(self, config):
        assert config.verify_ws_token("wrong") is False

    def test_verify_ws_token_none(self, config):
        assert config.verify_ws_token(None) is False

    def test_missing_env_var_raises(self, monkeypatch):
        """AuthConfig requires all env vars — missing one raises KeyError."""
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "x")
        # Don't set the rest
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("ALLOWED_EMAIL", raising=False)
        monkeypatch.delenv("AUTH_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("SESSION_SECRET", raising=False)
        monkeypatch.delenv("BASE_URL", raising=False)
        with pytest.raises(KeyError):
            AuthConfig()
