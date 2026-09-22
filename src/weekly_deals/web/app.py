"""Local web application.

Binding to loopback is not a security boundary. Any page in any browser on this
machine can send requests to 127.0.0.1, so this app checks Host and Origin,
issues a session cookie, and requires a CSRF token on every state-changing
request. Without those, a random web page could mark the user's offers as used.
"""

from __future__ import annotations

import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..service import WeeklyDealsService

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
SESSION_COOKIE = "weekly_deals_session"
CSRF_HEADER = "x-weekly-deals-csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _host_allowed(host_header: str | None) -> bool:
    if not host_header:
        return False
    host = host_header.rsplit(":", 1)[0] if host_header.count(":") <= 1 else host_header
    if host_header.startswith("["):
        host = host_header.split("]")[0] + "]"
    return host in ALLOWED_HOSTS


def create_app(service: WeeklyDealsService) -> FastAPI:
    app = FastAPI(
        title="Weekly Deals",
        docs_url=None,  # no interactive docs on a local app
        redoc_url=None,
        openapi_url=None,
    )
    app.state.service = service
    # Per-process secret: restarting invalidates old sessions, which is fine for
    # a single-user local tool and avoids persisting yet another secret.
    app.state.csrf_token = secrets.token_urlsafe(32)

    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        if not _host_allowed(request.headers.get("host")):
            return JSONResponse({"error": "host_not_allowed"}, status_code=400)

        origin = request.headers.get("origin")
        if origin is not None:
            from urllib.parse import urlparse

            parsed = urlparse(origin)
            if parsed.hostname not in ALLOWED_HOSTS:
                return JSONResponse({"error": "origin_not_allowed"}, status_code=403)

        if request.method not in SAFE_METHODS:
            token = request.headers.get(CSRF_HEADER)
            if not token or not secrets.compare_digest(token, app.state.csrf_token):
                return JSONResponse({"error": "csrf_failed"}, status_code=403)

        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        # No remote images, no inline scripts, no third-party anything.
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; "
            "form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        if SESSION_COOKIE not in request.cookies:
            response.set_cookie(
                SESSION_COOKIE,
                secrets.token_urlsafe(24),
                httponly=True,
                samesite="strict",
                secure=False,  # loopback http
            )
        return response

    from .routes import register

    register(app)
    return app
