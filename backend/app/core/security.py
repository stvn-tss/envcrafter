"""Request-level security guards shared by HTTP and WebSocket endpoints."""

from collections.abc import Sequence

from fastapi import HTTPException, Request, status
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings


def is_origin_allowed(origin: str | None, allowed_origins: Sequence[str]) -> bool:
    """Return True if a request's Origin header is acceptable.

    Browsers always attach Origin to WebSocket handshakes and to POST/DELETE
    requests, and page scripts cannot forge it. A missing Origin therefore means
    a non-browser client (CLI, tests), which cannot be the vector of a
    cross-site attack. Non-browser clients will be covered by authentication.
    """
    return origin is None or origin in allowed_origins


async def require_trusted_origin(request: Request) -> None:
    """Guard for every state-changing endpoint: blocks cross-site request
    forgery from any page the user visits while EnvCrafter runs on localhost."""
    settings: Settings = request.app.state.settings
    if not is_origin_allowed(request.headers.get("origin"), settings.allowed_origins):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Origin not allowed")


async def require_trusted_json_request(request: Request) -> None:
    """Origin check + strict `application/json`.

    A cross-site `fetch(..., {mode: "no-cors"})` can send a body with no
    Content-Type, which FastAPI would still parse as JSON. Requiring the header
    forces a CORS preflight, which we never grant.
    """
    await require_trusted_origin(request)
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json"
        )


_SECURITY_HEADERS = {
    # Browsers must not guess a script or HTML type from content.
    "X-Content-Type-Options": "nosniff",
    # No framing: a hostile page cannot overlay the Stop / Remove buttons (clickjacking).
    "X-Frame-Options": "DENY",
    # Environment addresses never leak through the Referer header.
    "Referrer-Policy": "no-referrer",
}


class SecurityHeadersMiddleware:
    """Adds baseline hardening headers to every HTTP response (pure ASGI)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
