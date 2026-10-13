"""Single-resident access control and safe frontend hosting for public deployments."""

import base64
import binascii
import os
from dataclasses import dataclass
from pathlib import PurePosixPath
from secrets import compare_digest
from urllib.parse import urlsplit

from starlette.datastructures import URL, Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_CHALLENGE = 'Basic realm="GuardMate", charset="UTF-8"'


@dataclass(frozen=True, repr=False)
class AccessSettings:
    username: str = ""
    password: str = ""
    render: bool = False

    @classmethod
    def from_environment(cls) -> "AccessSettings":
        settings = cls(
            username=os.environ.get("GUARDMATE_ACCESS_USERNAME", ""),
            password=os.environ.get("GUARDMATE_ACCESS_PASSWORD", ""),
            render=bool(os.environ.get("RENDER")),
        )
        if not settings.username and not settings.password and not settings.render:
            return settings
        if not settings.username or not settings.password:
            raise RuntimeError(
                "Set both GUARDMATE_ACCESS_USERNAME and GUARDMATE_ACCESS_PASSWORD "
                "before exposing GuardMate. Access credentials are required on Render."
            )
        if (
            settings.username != settings.username.strip()
            or ":" in settings.username
            or len(settings.username) > 128
            or any(ord(character) < 32 for character in settings.username)
        ):
            raise RuntimeError("GUARDMATE_ACCESS_USERNAME must be a valid HTTP Basic username.")
        password_classes = sum(
            (
                any(character.islower() for character in settings.password),
                any(character.isupper() for character in settings.password),
                any(character.isdigit() for character in settings.password),
                any(not character.isalnum() for character in settings.password),
            )
        )
        if (
            not 20 <= len(settings.password) <= 512
            or any(character.isspace() for character in settings.password)
            or password_classes < 3
            or len(set(settings.password)) < 10
        ):
            raise RuntimeError(
                "GUARDMATE_ACCESS_PASSWORD must be 20-512 characters without whitespace, "
                "with at least three character classes and ten distinct characters. "
                "Use a randomly generated password."
            )
        return settings

    @property
    def enabled(self) -> bool:
        return bool(self.username and self.password)

    def accepts(self, authorization: str) -> bool:
        if len(authorization) > 4096:
            return False
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (ValueError, UnicodeError, binascii.Error):
            return False
        username, separator, password = decoded.partition(":")
        if not separator:
            return False
        # Evaluate both comparisons, including when the username is wrong.
        username_matches = compare_digest(username.encode("utf-8"), self.username.encode("utf-8"))
        password_matches = compare_digest(password.encode("utf-8"), self.password.encode("utf-8"))
        return username_matches and password_matches


def _origin(value: str) -> str | None:
    try:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path
            or url.query
            or url.fragment
        ):
            return None
        port = url.port or (443 if url.scheme == "https" else 80)
        return f"{url.scheme}://{url.hostname.lower()}:{port}"
    except ValueError:
        return None


class AccessMiddleware:
    def __init__(self, app: ASGIApp, settings: AccessSettings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        public_health = scope["method"] == "GET" and scope["path"] == "/api/health"

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Content-Type-Options"] = "nosniff"
                response_headers["X-Frame-Options"] = "DENY"
                response_headers["Content-Security-Policy"] = (
                    "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
                )
                response_headers["Referrer-Policy"] = "no-referrer"
                response_headers["Permissions-Policy"] = (
                    "camera=(), geolocation=(), microphone=(self)"
                )
                response_headers["Cache-Control"] = "private, no-store"
            await send(message)

        if self.settings.enabled and not public_health:
            if not self.settings.accepts(headers.get("authorization", "")):
                response = JSONResponse(
                    {"detail": "Sign in with your GuardMate access username and password."},
                    status_code=401,
                    headers={"WWW-Authenticate": _CHALLENGE},
                )
                await response(scope, receive, secure_send)
                return
        if self.settings.enabled and scope["method"] not in _SAFE_METHODS:
            supplied_origin = headers.get("origin")
            expected_origin = _origin(str(URL(scope=scope).replace(path="", query="")))
            actual_origin = _origin(supplied_origin) if supplied_origin is not None else None
            if (
                supplied_origin is not None
                and (
                    actual_origin is None
                    or expected_origin is None
                    or not compare_digest(
                        actual_origin.encode("utf-8"), expected_origin.encode("utf-8")
                    )
                )
            ) or headers.get("sec-fetch-site") == "cross-site":
                response = JSONResponse(
                    {
                        "detail": "Open GuardMate directly to make changes; cross-origin requests "
                        "are not allowed."
                    },
                    status_code=403,
                )
                await response(scope, receive, secure_send)
                return
        await self.app(scope, receive, secure_send)


class FrontendFiles(StaticFiles):
    """Serve Vite output, reserving API paths and refusing unsafe/missing asset paths."""

    async def _static_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        # Windows registry MIME mappings can identify JavaScript as text/plain.
        # Explicit build asset types keep nosniff compatible with every supported host.
        media_type = {
            ".js": "text/javascript; charset=utf-8",
            ".mjs": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".html": "text/html; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(PurePosixPath(path).suffix.lower())
        if media_type is not None:
            response.headers["Content-Type"] = media_type
        return response

    async def get_response(self, path: str, scope: Scope) -> Response:
        request_path = scope["path"]
        parts = request_path.strip("/").split("/")
        if (
            "\\" in request_path
            or "\x00" in request_path
            or any(part.startswith(".") for part in parts)
            or parts[0] == "api"
        ):
            raise HTTPException(404)
        try:
            return await self._static_response(path, scope)
        except HTTPException as error:
            if error.status_code != 404:
                raise
            accept = Headers(scope=scope).get("accept", "")
            if (
                PurePosixPath(request_path).suffix
                or parts[0] == "assets"
                or not ("text/html" in accept or "*/*" in accept)
            ):
                raise
            return await self._static_response("index.html", scope)
