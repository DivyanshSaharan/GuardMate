from base64 import b64encode

import mongomock
import pytest
from fastapi.testclient import TestClient
from guardmate import main
from pymongo.errors import ServerSelectionTimeoutError

USERNAME = "demo-resident"
PASSWORD = "TestOnly-Random-Access-83xZ74pq"


def login_headers():
    return {"Authorization": "Basic " + b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()}


@pytest.fixture(autouse=True)
def isolated_access_environment(monkeypatch):
    monkeypatch.setattr(main, "load_dotenv", lambda *args, **kwargs: None)
    for name in ("GUARDMATE_ACCESS_USERNAME", "GUARDMATE_ACCESS_PASSWORD", "RENDER", "MONGODB_URI"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def configured_access(monkeypatch):
    monkeypatch.setenv("GUARDMATE_ACCESS_USERNAME", USERNAME)
    monkeypatch.setenv("GUARDMATE_ACCESS_PASSWORD", PASSWORD)


@pytest.fixture
def frontend(tmp_path):
    directory = tmp_path / "frontend"
    directory.mkdir()
    (directory / "index.html").write_text("<html><body>GuardMate shell</body></html>")
    (directory / "assets").mkdir()
    (directory / "assets" / "app.js").write_text("console.log('GuardMate')")
    return directory


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("", ""),
        (USERNAME, ""),
        ("", PASSWORD),
        (USERNAME, "short"),
        (USERNAME, "longbutonlylowercasepassword"),
        (USERNAME, "A1!" * 10),
        ("invalid:username", PASSWORD),
    ],
)
def test_render_refuses_missing_partial_or_weak_credentials(
    tmp_path, monkeypatch, username, password
):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("GUARDMATE_ACCESS_USERNAME", username)
    monkeypatch.setenv("GUARDMATE_ACCESS_PASSWORD", password)
    with pytest.raises(RuntimeError, match="GUARDMATE_ACCESS") as error:
        main.create_app(tmp_path)
    assert not password or password not in str(error.value)
    assert not (tmp_path / "guardmate.sqlite3").exists()


def test_local_development_without_credentials_keeps_working(tmp_path):
    with TestClient(main.create_app(tmp_path)) as client:
        assert client.get("/api/dashboard").status_code == 200
        assert client.put("/api/profile", json={"resident_name": "Local"}).status_code == 200


def test_render_requires_durable_mongodb_storage(tmp_path, configured_access, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(RuntimeError, match="MONGODB_URI"):
        main.create_app(tmp_path)
    assert not (tmp_path / "guardmate.sqlite3").exists()


def test_only_get_health_is_public_and_contains_no_resident_data(tmp_path, configured_access):
    with TestClient(main.create_app(tmp_path)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "service": "guardmate"}
        assert client.head("/api/health").status_code == 401
        assert client.post("/api/health").status_code == 401
        assert client.get("/api/health/").status_code == 401


@pytest.mark.parametrize("path", ["/", "/api/dashboard", "/docs", "/redoc", "/openapi.json"])
def test_api_docs_and_frontend_require_browser_login(tmp_path, configured_access, frontend, path):
    with TestClient(main.create_app(tmp_path / "data", frontend_dir=frontend)) as client:
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"].startswith('Basic realm="GuardMate"')
        assert "username and password" in response.json()["detail"]
        assert PASSWORD not in response.text
        assert client.get(path, auth=(USERNAME, PASSWORD)).status_code == 200


@pytest.mark.parametrize(
    "authorization",
    [
        "",
        "Bearer token",
        "Basic invalid!",
        "Basic " + b64encode(b"missing-colon").decode(),
        "Basic " + b64encode(b"\xff:invalid").decode(),
        "Basic " + b64encode(f"{USERNAME}:wrong-password".encode()).decode(),
        "Basic " + b64encode(f"wrong-user:{PASSWORD}".encode()).decode(),
    ],
)
def test_invalid_credentials_cannot_read_or_change_profile(
    tmp_path, configured_access, authorization
):
    with TestClient(main.create_app(tmp_path)) as client:
        headers = {"Authorization": authorization}
        assert client.get("/api/dashboard", headers=headers).status_code == 401
        assert (
            client.put(
                "/api/profile", json={"resident_name": "Intruder"}, headers=headers
            ).status_code
            == 401
        )
        dashboard = client.get("/api/dashboard", auth=(USERNAME, PASSWORD)).json()
        assert dashboard["profile"]["resident_name"] == ""


@pytest.mark.parametrize(
    "origin",
    [
        "http://attacker.test",
        "null",
        "https://testserver",
        "http://testserver/",
        "http://testserver:invalid",
        "http://usér.test",
    ],
)
def test_authenticated_cross_origin_changes_are_rejected(tmp_path, configured_access, origin):
    with TestClient(main.create_app(tmp_path), headers=login_headers()) as client:
        response = client.put(
            "/api/profile",
            json={"resident_name": "Intruder"},
            headers={"Origin": origin.encode("utf-8")},
        )
        assert response.status_code == 403
        assert client.get("/api/dashboard").json()["profile"]["resident_name"] == ""


@pytest.mark.parametrize("origin", [None, "http://testserver", "http://testserver:80"])
def test_authenticated_same_origin_and_cli_changes_work(tmp_path, configured_access, origin):
    with TestClient(main.create_app(tmp_path), headers=login_headers()) as client:
        headers = {"Origin": origin} if origin is not None else {}
        response = client.put("/api/profile", json={"resident_name": "Resident"}, headers=headers)
        assert response.status_code == 200
        assert response.json()["profile"]["resident_name"] == "Resident"


def test_https_origin_matches_forwarded_asgi_scheme(tmp_path, configured_access):
    with TestClient(
        main.create_app(tmp_path), base_url="https://guardmate.example", headers=login_headers()
    ) as client:
        response = client.put(
            "/api/profile",
            json={"resident_name": "Resident"},
            headers={"Origin": "https://guardmate.example"},
        )
        assert response.status_code == 200


def test_browser_cross_site_signal_cannot_bypass_origin_check(tmp_path, configured_access):
    with TestClient(main.create_app(tmp_path), headers=login_headers()) as client:
        response = client.put(
            "/api/profile",
            json={"resident_name": "Intruder"},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert response.status_code == 403


@pytest.mark.parametrize("path", ["/", "/api/dashboard", "/api/health"])
def test_sensitive_and_public_responses_have_browser_security_headers(
    tmp_path, configured_access, frontend, path
):
    with TestClient(main.create_app(tmp_path / "data", frontend_dir=frontend)) as client:
        for auth in (None, (USERNAME, PASSWORD)):
            response = client.get(path, auth=auth)
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["X-Frame-Options"] == "DENY"
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert response.headers["Cache-Control"] == "private, no-store"


def test_frontend_assets_and_html_navigation_are_served(tmp_path, configured_access, frontend):
    with TestClient(
        main.create_app(tmp_path / "data", frontend_dir=frontend), headers=login_headers()
    ) as client:
        assert "GuardMate shell" in client.get("/").text
        assert (
            "GuardMate shell"
            in client.get("/delivery/settings", headers={"Accept": "text/html"}).text
        )
        asset = client.get("/assets/app.js")
        assert asset.status_code == 200
        assert "console.log" in asset.text
        assert "javascript" in asset.headers["Content-Type"]
        assert client.head("/delivery/settings").status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api",
        "/api/unknown",
        "/api/health/",
        "/assets/missing.js",
        "/assets/missing",
        "/missing.txt",
        "/.env",
        "/%2e%2e/secret.txt",
        "/assets/%2e%2e/%2e%2e/secret.txt",
        "/assets/..%5c..%5csecret.txt",
    ],
)
def test_unknown_api_assets_and_traversal_never_receive_spa_html(
    tmp_path, configured_access, frontend, path
):
    (tmp_path / "secret.txt").write_text("do not serve this")
    with TestClient(
        main.create_app(tmp_path / "data", frontend_dir=frontend), headers=login_headers()
    ) as client:
        response = client.get(path, headers={"Accept": "text/html"})
        assert response.status_code == 404
        assert "GuardMate shell" not in response.text
        assert "do not serve this" not in response.text


def test_missing_non_html_navigation_and_post_do_not_receive_spa_html(
    tmp_path, configured_access, frontend
):
    with TestClient(
        main.create_app(tmp_path / "data", frontend_dir=frontend), headers=login_headers()
    ) as client:
        assert client.get("/missing", headers={"Accept": "application/json"}).status_code == 404
        assert client.post("/missing").status_code == 405


def test_unbuilt_frontend_does_not_break_backend(tmp_path, configured_access):
    with TestClient(
        main.create_app(tmp_path / "data", frontend_dir=tmp_path / "missing"),
        headers=login_headers(),
    ) as client:
        assert client.get("/").status_code == 404
        assert client.get("/api/dashboard").status_code == 200


@pytest.fixture
def mongo_connection(monkeypatch):
    class TestConnection:
        database = mongomock.MongoClient()["guardmate"]
        available = True
        closed = False

        def ping(self):
            if not self.available:
                raise ServerSelectionTimeoutError("private database connection details")

        def close(self):
            self.closed = True

    connection = TestConnection()
    monkeypatch.setattr(main.MongoPersistence, "from_environment", lambda: connection)
    return connection


def test_public_health_checks_database_readiness_without_revealing_connection_details(
    tmp_path, configured_access, mongo_connection
):
    with TestClient(main.create_app(tmp_path)) as client:
        assert client.get("/api/health").json() == {"status": "ok", "service": "guardmate"}
        mongo_connection.available = False
        response = client.get("/api/health")
        assert response.status_code == 503
        assert response.json() == {"detail": "GuardMate storage is temporarily unavailable."}
        assert "private database" not in response.text
        assert response.headers["Cache-Control"] == "private, no-store"
        mongo_connection.available = True
        assert client.get("/api/health").status_code == 200
    assert mongo_connection.closed
    assert not (tmp_path / "guardmate.sqlite3").exists()


def test_failed_database_startup_is_sanitized_and_closes_the_connection(
    tmp_path, configured_access, mongo_connection
):
    mongo_connection.available = False
    with pytest.raises(RuntimeError, match="storage could not be reached") as error:
        with TestClient(main.create_app(tmp_path)):
            pass
    assert "private database" not in str(error.value)
    assert mongo_connection.closed


def test_database_failure_in_authenticated_routes_returns_a_safe_unavailable_response(
    tmp_path, configured_access, mongo_connection, monkeypatch
):
    application = main.create_app(tmp_path)
    with TestClient(application, headers=login_headers()) as client:

        def failed_read(_key):
            raise ServerSelectionTimeoutError("private database connection details")

        monkeypatch.setattr(application.state.store, "read", failed_read)
        response = client.get("/api/dashboard")
        assert response.status_code == 503
        assert "private database" not in response.text
        assert response.json() == {"detail": "GuardMate storage is temporarily unavailable."}
