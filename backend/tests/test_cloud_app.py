from types import SimpleNamespace
from unittest.mock import Mock

import mongomock
from fastapi.testclient import TestClient
from guardmate.cloud_app import create_cloud_app


def persistence():
    return SimpleNamespace(
        database=mongomock.MongoClient(tz_aware=True).guardmate,
        ping=Mock(),
        close=Mock(),
    )


def test_cloud_frontend_is_public_but_never_mounts_legacy_resident_api(tmp_path, monkeypatch):
    monkeypatch.setenv("GUARDMATE_ACCESS_USERNAME", "old-dashboard-user")
    monkeypatch.setenv("GUARDMATE_ACCESS_PASSWORD", "legacy-value-must-not-enable-basic")
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html>GuardMate public website</html>")
    storage = persistence()
    storage.database.preferences.insert_one({"_id": "profile", "value": "private-old-resident"})
    with TestClient(create_cloud_app(frontend, persistence=storage)) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "WWW-Authenticate" not in response.headers
        assert "private-old-resident" not in response.text
        assert client.get("/api/runtime").json()["mode"] == "cloud"
        for path in (
            "/api/dashboard",
            "/api/conversations",
            "/api/desktop/status",
            "/api/speech/status",
            "/openapi.json",
        ):
            assert client.get(path).status_code == 404
        assert "swagger-ui" not in client.get("/docs").text
        assert client.get("/api/cloud/config").json()["oauth_ready"] is False
        assert client.get("/api/cloud/download/windows").status_code == 401
    assert (
        storage.database.preferences.find_one({"_id": "profile"})["value"] == "private-old-resident"
    )
    storage.close.assert_called_once()


def test_partial_download_settings_do_not_enable_installer(tmp_path, monkeypatch):
    monkeypatch.setenv("GUARDMATE_DESKTOP_RELEASE", '{"invalid":true}')
    monkeypatch.setenv("GUARDMATE_INSTALLER_RELEASE", "{}")
    with TestClient(create_cloud_app(tmp_path, persistence=persistence())) as client:
        assert client.get("/api/cloud/config").json()["download_ready"] is False
