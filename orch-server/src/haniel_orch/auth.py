"""Authentication for the orchestrator server.

Provides Google OAuth login, HMAC-signed session tokens, and Bearer token
verification for API and WebSocket access control.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import urlencode

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

SESSION_COOKIE = "orch_session"
SESSION_MAX_AGE = 86400 * 7  # 7 days


class AuthConfig:
    """Authentication configuration. All auth-related env vars are required."""

    def __init__(self) -> None:
        self.google_client_id: str = os.environ["GOOGLE_CLIENT_ID"]
        self.google_client_secret: str = os.environ["GOOGLE_CLIENT_SECRET"]
        self.allowed_email: str = os.environ["ALLOWED_EMAIL"]
        self.bearer_token: str = os.environ["AUTH_BEARER_TOKEN"]
        self.session_secret: str = os.environ["SESSION_SECRET"]
        self.base_url: str = os.environ["BASE_URL"]

    def create_session_token(self, email: str) -> str:
        """Create HMAC-signed session token."""
        payload = json.dumps({"email": email, "exp": int(time.time()) + SESSION_MAX_AGE})
        sig = hmac.new(
            self.session_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return base64.urlsafe_b64encode(payload.encode()).decode() + "." + sig

    def verify_session_token(self, token: str) -> str | None:
        """Verify token and return email, or None if invalid/expired."""
        try:
            payload_b64, sig = token.rsplit(".", 1)
            payload = base64.urlsafe_b64decode(payload_b64).decode()
            expected_sig = hmac.new(
                self.session_secret.encode(), payload.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected_sig):
                return None
            data = json.loads(payload)
            if data.get("exp", 0) < time.time():
                return None
            return data.get("email")
        except Exception:
            return None

    def verify_bearer(self, request: Request) -> bool:
        """Verify Authorization: Bearer header matches AUTH_BEARER_TOKEN."""
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return hmac.compare_digest(auth[7:], self.bearer_token)
        return False

    def verify_ws_token(self, token: str | None) -> bool:
        """Verify WebSocket query param token matches AUTH_BEARER_TOKEN."""
        if not token:
            return False
        return hmac.compare_digest(token, self.bearer_token)


def create_auth_routes(config: AuthConfig) -> list[Route]:
    """Create authentication routes for Google OAuth flow."""

    async def login(request: Request) -> Response:
        """Redirect to Google OAuth consent screen."""
        redirect_uri = f"{config.base_url}/auth/callback"
        params = {
            "client_id": config.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email",
            "access_type": "offline",
            "prompt": "select_account",
        }
        url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
        return RedirectResponse(url)

    async def callback(request: Request) -> Response:
        """Handle Google OAuth callback: exchange code, verify email, set cookie."""
        code = request.query_params.get("code")
        if not code:
            return JSONResponse({"error": "missing code"}, status_code=400)

        redirect_uri = f"{config.base_url}/auth/callback"

        # Exchange code for tokens
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": config.google_client_id,
                    "client_secret": config.google_client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )

        if token_resp.status_code != 200:
            logger.warning(f"OAuth token exchange failed: {token_resp.text}")
            return JSONResponse({"error": "token exchange failed"}, status_code=401)

        token_data = token_resp.json()
        access_token = token_data.get("access_token")

        # Get user info
        async with httpx.AsyncClient() as client:
            userinfo_resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )

        if userinfo_resp.status_code != 200:
            return JSONResponse({"error": "failed to get user info"}, status_code=401)

        userinfo = userinfo_resp.json()
        email = userinfo.get("email", "")

        # Check allowed email
        if email != config.allowed_email:
            logger.warning(f"OAuth login rejected for: {email}")
            return JSONResponse({"error": "access denied"}, status_code=403)

        # Create session token and set cookie
        session_token = config.create_session_token(email)

        # Redirect to dashboard with token for localStorage
        response = RedirectResponse(
            f"/dashboard?token={config.bearer_token}", status_code=302
        )
        response.set_cookie(
            SESSION_COOKIE,
            session_token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=config.base_url.startswith("https"),
        )
        return response

    async def logout(request: Request) -> Response:
        """Clear session cookie and redirect to login."""
        response = RedirectResponse("/auth/login")
        response.delete_cookie(SESSION_COOKIE)
        return response

    return [
        Route("/auth/login", login),
        Route("/auth/callback", callback),
        Route("/auth/logout", logout),
    ]
