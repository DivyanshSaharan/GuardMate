"""A single-use browser handoff and authenticated, literal-loopback local API."""

import re
from http.cookies import SimpleCookie
from secrets import compare_digest
from threading import Lock
from urllib.parse import parse_qs

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, RedirectResponse

COOKIE = "guardmate_local"
HOST = re.compile(r"(?:127\.0\.0\.1|\[::1\]):[1-9][0-9]{0,4}\Z")


class LocalAccessMiddleware:
    def __init__(self, app, *, token: str, ticket: str):
        self.app, self.token, self.ticket = app, token, ticket
        self._available, self._lock = True, Lock()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        client = scope.get("client")
        if (
            HOST.fullmatch(host) is None
            or client is None
            or client[0] not in ("127.0.0.1", "::1")
            or headers.get("sec-fetch-site") == "cross-site"
            or headers.get("origin", "http://" + host) != "http://" + host
        ):
            await JSONResponse({"detail": "Open the local GuardMate application directly."}, 403)(
                scope, receive, send
            )
            return
        if scope["path"] == "/desktop/open" and scope["method"] == "GET":
            supplied = parse_qs(scope.get("query_string", b"").decode(errors="replace")).get(
                "ticket", [""]
            )[0]
            with self._lock:
                valid = self._available and compare_digest(supplied, self.ticket)
                if valid:
                    self._available = False
            if valid:
                response = RedirectResponse("/", status_code=303)
                response.set_cookie(COOKIE, self.token, httponly=True, samesite="strict", path="/")
                response.headers["Cache-Control"] = "no-store"
                response.headers["Referrer-Policy"] = "no-referrer"
                await response(scope, receive, send)
                return
        jar = SimpleCookie()
        try:
            jar.load(headers.get("cookie", ""))
            cookie = jar[COOKIE].value if COOKIE in jar else ""
        except Exception:
            cookie = ""
        authorization = headers.get("authorization", "")
        bearer = authorization[7:] if authorization.startswith("Bearer ") else ""
        valid = compare_digest(cookie, self.token) or compare_digest(bearer, self.token)
        public_health = scope["path"] == "/api/health" and scope["method"] == "GET"
        if not valid and not public_health:
            await JSONResponse({"detail": "Open GuardMate using its desktop shortcut."}, 401)(
                scope, receive, send
            )
            return
        await self.app(scope, receive, send)
