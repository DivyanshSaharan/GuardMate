import json
import time
from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from guardmate.agent.provider import ModelUnavailable
from guardmate.cloud.auth import GoogleSettings
from guardmate.desktop.local import LocalAccessMiddleware
from guardmate.desktop.remote import RemoteProvider


def session():
    return {
        "version": 1,
        "server_url": "https://guardmate.onrender.com",
        "bootstrap_token": "test-activation-secret",
        "access_token": "test-session-secret",
        "account_id": "test-account",
        "expires_at": time.time() + 900,
    }


def test_remote_refresh_keeps_account_and_does_not_issue_paid_call(tmp_path):
    original = session()
    original["expires_at"] = 0
    saved, calls = [], []

    def transport(path, body=None, token=None):
        calls.append(path)
        if path == "/activate":
            assert body == {"credential": original["bootstrap_token"]}
            return {
                "access_token": "test-renewed-session-token",
                "account_id": "test-account",
                "expires_in": 900,
            }
        return {
            "account_id": "test-account",
            "budget_microdollars": 1_000_000,
            "reserved_microdollars": 300_000,
            "remaining_microdollars": 700_000,
        }

    provider = RemoteProvider(
        tmp_path,
        transport=transport,
        session_loader=lambda: original,
        session_saver=lambda value: saved.append(deepcopy(value)),
    )
    assert provider.account()["remaining_microdollars"] == 700_000
    assert calls == ["/activate", "/account"]
    assert saved[0]["account_id"] == original["account_id"]
    assert saved[0]["expires_at"] > time.time()


def test_remote_unknown_outcome_records_identifier_without_retry_or_secrets(tmp_path):
    calls = []

    def transport(path, body=None, token=None):
        calls.append(body["request_id"])
        raise ModelUnavailable("transport timeout")

    provider = RemoteProvider(tmp_path, transport=transport, session_loader=session)
    with pytest.raises(ModelUnavailable, match="transport timeout"):
        provider.generate([{"role": "user", "content": "private fictional transcript"}])
    assert len(calls) == 1
    journal = list((tmp_path / "data" / "inference").glob("*.json"))
    assert len(journal) == 1
    content = journal[0].read_text()
    assert json.loads(content)["state"] == "uncertain"
    assert "private fictional transcript" not in content
    assert "secret" not in content


def test_remote_rejects_changed_account_before_model_call(tmp_path):
    expired = session()
    expired["expires_at"] = 0
    calls = []

    def transport(path, body=None, token=None):
        calls.append(path)
        return {"account_id": "someone-else"}

    provider = RemoteProvider(tmp_path, transport=transport, session_loader=lambda: expired)
    with pytest.raises(ModelUnavailable, match="identity changed"):
        provider.generate([{"role": "user", "content": "fictional"}])
    assert calls == ["/activate"]


def test_remote_cannot_send_credential_to_another_origin(tmp_path):
    invalid = session()
    invalid["server_url"] = "https://example.test"
    provider = RemoteProvider(
        tmp_path,
        transport=lambda *args, **kwargs: pytest.fail("network"),
        session_loader=lambda: invalid,
    )
    with pytest.raises(ModelUnavailable, match="unsupported"):
        provider.account()


def local_client():
    application = FastAPI()
    application.add_middleware(LocalAccessMiddleware, token="private-key", ticket="once")

    @application.get("/api/runtime")
    def runtime():
        return {"mode": "desktop"}

    return TestClient(application, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 12345))


def test_local_browser_ticket_is_single_use_and_sets_private_cookie():
    with local_client() as client:
        assert client.get("/api/runtime").status_code == 401
        handoff = client.get("/desktop/open?ticket=once", follow_redirects=False)
        assert handoff.status_code == 303
        assert "HttpOnly" in handoff.headers["set-cookie"]
        assert "SameSite=strict" in handoff.headers["set-cookie"]
        assert handoff.headers["location"] == "/"
        assert client.get("/api/runtime").json()["mode"] == "desktop"
        client.cookies.clear()
        assert client.get("/desktop/open?ticket=once", follow_redirects=False).status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "attacker.test:8765"},
        {"Origin": "https://attacker.test"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_local_api_refuses_rebinding_and_cross_origin_even_with_token(headers):
    with local_client() as client:
        assert (
            client.get(
                "/api/runtime",
                headers={
                    "Authorization": "Bearer private-key",
                    **headers,
                },
            ).status_code
            == 403
        )


def test_local_google_callback_only_allows_loopback_http():
    settings = GoogleSettings("client", "secret", "http://localhost:8765")
    assert settings.callback_url == "http://localhost:8765/api/cloud/auth/google/callback"
    with pytest.raises(ValueError):
        GoogleSettings("client", "secret", "http://attacker.test:8765")
