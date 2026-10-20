"""One-attempt HTTPS planner client. Owner/provider/database secrets stay on Render."""

import hashlib
import http.client
import json
import time
from pathlib import Path
from threading import Lock
from uuid import uuid4

from pydantic import ValidationError

from ..agent.models import AgentPlan, ModelStatus
from ..agent.provider import MODEL, ModelUnavailable
from .config import DesktopConfigError, load_session, save_session

ORIGIN = "https://guardmate.onrender.com"
MAX_RESPONSE = 256_000
MESSAGES = {
    "inference_busy": "GuardMate is helping another request. Wait a moment before another turn.",
    "account_budget_exhausted": "Your account allowance is exhausted.",
    "sign_in_required": "Run your account's installer again to reconnect GuardMate.",
    "invalid_credential": (
        "This installer credential was revoked. Download a new copy after sign-in."
    ),
    "request_outcome_uncertain": "This AI request has an uncertain outcome and was not retried.",
    "request_already_completed": "This request already completed. Refresh the conversation.",
}


class RemoteProvider:
    def __init__(self, root: Path, *, transport=None, session_loader=None, session_saver=None):
        self.root = root
        self._transport = transport or self._https
        self._load = session_loader or (lambda: load_session(root))
        self._save = session_saver or (lambda session: save_session(session, root))
        self._lock = Lock()
        self._budget = {"reserved_microdollars": 0, "budget_microdollars": 1_000_000}
        self._checked_at = 0.0

    @staticmethod
    def _https(path, body=None, token=None):
        connection = http.client.HTTPSConnection(
            "guardmate.onrender.com", timeout=65 if path == "/inference" else 10
        )
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        try:
            connection.request(
                "GET" if body is None else "POST",
                "/api/cloud" + path,
                body=None if body is None else json.dumps(body, allow_nan=False).encode(),
                headers=headers,
            )
            response = connection.getresponse()
            data = response.read(MAX_RESPONSE + 1)
            if (
                len(data) > MAX_RESPONSE
                or response.getheader("Content-Type", "").split(";")[0] != "application/json"
            ):
                raise ModelUnavailable("GuardMate returned an invalid response. No retry was made.")
            parsed = json.loads(data)
            if response.status != 200:
                raise ModelUnavailable(
                    MESSAGES.get(parsed.get("detail"), "GuardMate could not complete the request.")
                )
            return parsed
        except ModelUnavailable:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise ModelUnavailable(
                "GuardMate could not confirm the request outcome. It was not retried."
            ) from None
        finally:
            connection.close()

    def _session(self, *, refresh=False):
        session = self._load()
        if session is None:
            raise ModelUnavailable(
                "Run your downloaded GuardMate installer to connect your account."
            )
        if session["server_url"].rstrip("/") != ORIGIN:
            raise ModelUnavailable("The desktop session is for an unsupported service.")
        if refresh and session["expires_at"] <= time.time() + 30:
            result = self._transport("/activate", {"credential": session["bootstrap_token"]})
            if result.get("account_id") != session["account_id"]:
                raise ModelUnavailable("Account identity changed. Run setup again.")
            token, expires = result.get("access_token"), result.get("expires_in")
            if not isinstance(token, str) or not 20 <= len(token) <= 4096:
                raise ModelUnavailable("GuardMate returned an invalid account session.")
            if type(expires) is not int or not 0 < expires <= 3600:
                raise ModelUnavailable("GuardMate returned an invalid session expiration.")
            session = {
                **session,
                "access_token": token,
                "expires_at": time.time() + expires,
            }
            self._save(session)
        return session

    def _update_budget(self, result):
        for key in ("reserved_microdollars", "budget_microdollars"):
            if type(result.get(key)) is not int or result[key] < 0:
                raise ModelUnavailable("GuardMate returned invalid account accounting.")
        self._budget = {key: result[key] for key in self._budget}

    def account(self):
        with self._lock:
            session = self._session(refresh=True)
            result = self._transport("/account", token=session["access_token"])
            if result.get("account_id") != session["account_id"]:
                raise ModelUnavailable("GuardMate returned a different account.")
            self._update_budget(result)
            self._checked_at = time.monotonic()
            return result

    def status(self) -> ModelStatus:
        try:
            configured = self._session() is not None
            if configured and time.monotonic() - self._checked_at > 60:
                self.account()
        except (DesktopConfigError, ModelUnavailable):
            configured = False
        return ModelStatus(
            configured=configured,
            model=MODEL,
            provider="GuardMate gateway · Tinker",
            target_kind="base",
            message=(
                "Account connected. Reviewed text and resident context go to GuardMate and Tinker."
                if configured
                else "Run your downloaded installer to connect your account."
            ),
            reserved_usd=self._budget["reserved_microdollars"] / 1_000_000,
            budget_usd=self._budget["budget_microdollars"] / 1_000_000,
        )

    def generate(self, messages: list[dict[str, str]]) -> AgentPlan:
        with self._lock:
            session = self._session(refresh=True)
            request_id = str(uuid4())
            body = {"request_id": request_id, "messages": messages}
            # Persist only an identifier/digest, before sending; never automatically resend.
            journal = self.root / "data" / "inference"
            journal.mkdir(parents=True, exist_ok=True)
            path = journal / (request_id + ".json")
            record = {
                "request_id": request_id,
                "account_id": session["account_id"],
                "digest": hashlib.sha256(json.dumps(messages).encode()).hexdigest(),
                "state": "dispatching",
            }
            path.write_text(json.dumps(record), encoding="utf-8")
            try:
                result = self._transport("/inference", body, token=session["access_token"])
                if result.get("request_id") != request_id:
                    raise ModelUnavailable("GuardMate returned a different request.")
                if result.get("account_id") != session["account_id"]:
                    raise ModelUnavailable("GuardMate returned a different account.")
                self._update_budget(result)
                plan = AgentPlan.model_validate(result["plan"])
            except ModelUnavailable:
                record["state"] = "uncertain"
                path.write_text(json.dumps(record), encoding="utf-8")
                raise
            except (ValidationError, KeyError, DesktopConfigError):
                record["state"] = "uncertain"
                path.write_text(json.dumps(record), encoding="utf-8")
                raise ModelUnavailable(
                    "The AI request could not be completed. It was not automatically retried; "
                    "any server reservation remains recorded."
                ) from None
            record["state"] = "completed"
            path.write_text(json.dumps(record), encoding="utf-8")
            return plan
