"""Bearer-token ASGI middleware.

Each request must carry `Authorization: Bearer <token>`. The token is compared
in constant time against every configured token; on success the identity is
exposed to tool handlers via the `current_identity` contextvar.
"""

from __future__ import annotations

import hmac
from contextvars import ContextVar

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import AppConfig, TokenConfig

current_identity: ContextVar[TokenConfig | None] = ContextVar(
    "current_identity", default=None
)


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, config: AppConfig) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        identity = self._resolve(scope)
        if identity is None:
            await _unauthorized(scope, receive, send)
            return

        token = current_identity.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            current_identity.reset(token)

    def _resolve(self, scope: Scope) -> TokenConfig | None:
        for key, value in scope.get("headers", []):
            if key == b"authorization":
                presented = value.decode("latin-1", "replace").strip()
                if not presented.lower().startswith("bearer "):
                    return None
                return _match_token(self.config, presented[7:].strip())
        return None


def _match_token(config: AppConfig, raw: str) -> TokenConfig | None:
    matched: TokenConfig | None = None
    raw_bytes = raw.encode("utf-8")
    for token in config.tokens:
        candidate = token.token.encode("utf-8")
        a, b = _pad_pair(raw_bytes, candidate)
        if hmac.compare_digest(a, b) and len(raw_bytes) == len(candidate):
            matched = token
            # Keep iterating so total time is independent of which (if any) hit.
    return matched


def _pad_pair(a: bytes, b: bytes) -> tuple[bytes, bytes]:
    n = max(len(a), len(b))
    return a.ljust(n, b"\0"), b.ljust(n, b"\0")


async def _unauthorized(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse(
        {"error": "unauthorized"},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="mysql-readonly-mcp"'},
    )
    await response(scope, receive, send)


def require_identity() -> TokenConfig:
    identity = current_identity.get()
    if identity is None:
        raise PermissionError("No authenticated identity on this request.")
    return identity
