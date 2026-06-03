"""看板访问控制:默认仅 loopback;public 时放行;password 非空则 HTTP Basic。

只作用于 guarded_prefixes(看板路由),不碰 /api/v1/team 等其它路由。
"""
from __future__ import annotations

import base64
import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class DashboardAccessMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, public: bool, password: str,
                 guarded_prefixes=("/", "/api/v1/dashboard")):
        super().__init__(app)
        self._public = public
        self._password = password or ""
        self._prefixes = tuple(guarded_prefixes)

    def _guarded(self, path: str) -> bool:
        return path == "/" or any(
            path.startswith(p) for p in self._prefixes if p != "/")

    async def dispatch(self, request, call_next):
        if not self._guarded(request.url.path):
            return await call_next(request)
        if not self._public:
            host = request.client.host if request.client else ""
            if host not in _LOOPBACK:
                return PlainTextResponse("dashboard is local-only", status_code=403)
        if self._password and not self._check_basic(request):
            return PlainTextResponse(
                "auth required", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="xskill"'})
        return await call_next(request)

    def _check_basic(self, request) -> bool:
        h = request.headers.get("authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            _, pw = base64.b64decode(h[6:]).decode().split(":", 1)
        except Exception:  # pylint: disable=broad-exception-caught
            return False
        return hmac.compare_digest(pw, self._password)
