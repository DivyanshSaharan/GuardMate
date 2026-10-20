"""Google authorization-code login with PKCE, browser binding and verified ID tokens."""

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

import httpx

from .store import CloudError, digest_token


@dataclass(frozen=True, repr=False)
class GoogleSettings:
    client_id: str
    client_secret: str
    public_origin: str

    def __post_init__(self):
        parsed = urlsplit(self.public_origin)
        loopback = (
            parsed.scheme == "http"
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and parsed.port is not None
        )
        if (
            (parsed.scheme != "https" and not loopback)
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Use HTTPS, or HTTP on an explicit loopback development port.")

    @property
    def callback_url(self):
        return self.public_origin + "/api/cloud/auth/google/callback"


def verify_google_token(token, audience):
    from google.auth.transport.requests import Request
    from google.oauth2.id_token import verify_oauth2_token

    class BoundedRequest(Request):
        def __call__(self, *args, **kwargs):
            kwargs["timeout"] = 10
            return super().__call__(*args, **kwargs)

    return verify_oauth2_token(token, BoundedRequest(), audience=audience)


class GoogleOAuth:
    def __init__(self, store, settings, *, exchange=None, verify=None):
        self.store, self.settings = store, settings
        self.exchange = exchange or self._exchange
        self.verify = verify or verify_google_token

    def _exchange(self, code, verifier):
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": self.settings.client_id,
                    "client_secret": self.settings.client_secret,
                    "code": code,
                    "code_verifier": verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": self.settings.callback_url,
                },
            )
            response.raise_for_status()
            return response.json()["id_token"]

    def start(self):
        state, browser, nonce = (secrets.token_urlsafe(32) for _ in range(3))
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(
            b"="
        )
        self.store.flows.insert_one(
            {
                "_id": digest_token(state),
                "browser": digest_token(browser),
                "nonce": nonce,
                "verifier": verifier,
                "expires_at": self.store.clock() + timedelta(minutes=5),
            }
        )
        query = urlencode(
            {
                "client_id": self.settings.client_id,
                "redirect_uri": self.settings.callback_url,
                "response_type": "code",
                "scope": "openid email",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge.decode(),
                "code_challenge_method": "S256",
            }
        )
        return "https://accounts.google.com/o/oauth2/v2/auth?" + query, browser

    def complete(self, code, state, browser):
        if not all(
            isinstance(value, str) and 1 <= len(value) <= 4096 for value in (code, state, browser)
        ):
            raise CloudError("invalid_oauth_state", 401)
        flow = self.store.flows.find_one_and_delete(
            {
                "_id": digest_token(state),
                "browser": digest_token(browser),
                "expires_at": {"$gt": self.store.clock()},
            }
        )
        if flow is None:
            raise CloudError("invalid_oauth_state", 401)
        try:
            claims = self.verify(self.exchange(code, flow["verifier"]), self.settings.client_id)
            if (
                claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}
                or claims.get("aud") != self.settings.client_id
                or claims.get("email_verified") is not True
                or not isinstance(claims.get("sub"), str)
                or not 1 <= len(claims["sub"]) <= 255
                or not isinstance(claims.get("nonce"), str)
                or not secrets.compare_digest(claims["nonce"], flow["nonce"])
            ):
                raise ValueError("Invalid identity claims")
            account = self.store.ensure_account(claims["sub"])
            return self.store.create_session(account["_id"])
        except Exception:
            raise CloudError("google_sign_in_failed", 401) from None
