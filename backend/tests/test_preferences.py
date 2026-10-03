from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from guardmate.main import create_app

PROFILE = {
    "resident_name": "Arjun",
    "pg_name": "Maple House PG",
    "guard_location": "the guard room beside the main entrance",
    "guard_directions": "Use the pedestrian gate.",
    "office_days": [0, 1, 2, 3, 4],
    "office_start": "09:00",
    "office_end": "19:00",
    "outside_office": "ask_me",
    "weekend": "ask_me",
    "timezone": "Asia/Kolkata",
}


def client_at(tmp_path, iso_time):
    clock = {"now": datetime.fromisoformat(iso_time)}
    app = create_app(tmp_path, clock=lambda: clock["now"])
    return TestClient(app), clock


def test_new_install_starts_disabled_and_requires_setup(tmp_path):
    with client_at(tmp_path, "2026-10-05T06:00:00+00:00")[0] as client:
        data = client.get("/api/dashboard").json()
        assert not data["context"]["setup_complete"]
        assert not data["context"]["delivery_mode_active"]
        assert not data["voice_connected"]
        response = client.put(
            "/api/delivery-mode",
            json={"enabled": True, "expires_at": "2026-10-05T14:00:00+00:00"},
        )
        assert response.status_code == 422


def test_preferences_persist_across_application_restarts(tmp_path):
    with client_at(tmp_path, "2026-10-05T06:00:00+00:00")[0] as client:
        response = client.put("/api/profile", json={**PROFILE, "resident_name": " Arjun "})
        assert response.status_code == 200
        assert response.json()["profile"]["resident_name"] == "Arjun"
    with client_at(tmp_path, "2026-10-05T06:00:00+00:00")[0] as restarted:
        data = restarted.get("/api/dashboard").json()
        assert data["profile"]["pg_name"] == PROFILE["pg_name"]
        assert PROFILE["guard_directions"] in data["context"]["instruction"]


@pytest.mark.parametrize(
    ("instant", "expected", "source"),
    [
        ("2026-10-05T03:29:00+00:00", "ask_me", "outside_office"),
        ("2026-10-05T03:30:00+00:00", "at_office", "office_hours"),
        ("2026-10-05T13:29:00+00:00", "at_office", "office_hours"),
        ("2026-10-05T13:30:00+00:00", "ask_me", "outside_office"),
        ("2026-10-04T06:00:00+00:00", "ask_me", "weekend"),
    ],
)
def test_schedule_obeys_ist_boundaries(tmp_path, instant, expected, source):
    with client_at(tmp_path, instant)[0] as client:
        data = client.put("/api/profile", json=PROFILE).json()
        assert data["context"]["availability"] == expected
        assert data["context"]["availability_source"] == source


def test_sunday_can_be_an_explicit_office_day(tmp_path):
    with client_at(tmp_path, "2026-10-04T06:00:00+00:00")[0] as client:
        data = client.put("/api/profile", json={**PROFILE, "office_days": [6]}).json()
        assert data["context"]["availability"] == "at_office"


def test_today_override_expires_at_local_midnight(tmp_path):
    client, clock = client_at(tmp_path, "2026-10-04T18:29:00+00:00")
    with client:
        client.put("/api/profile", json=PROFILE)
        data = client.put("/api/availability", json={"status": "at_pg"}).json()
        assert data["context"]["availability_source"] == "today"
        assert "personally" in data["context"]["instruction"]
        clock["now"] = datetime(2026, 10, 4, 18, 30, tzinfo=UTC)
        data = client.get("/api/dashboard").json()
        assert data["context"]["today_override"] is None
        assert data["context"]["availability_source"] == "outside_office"


def test_clearing_today_override_restores_routine(tmp_path):
    with client_at(tmp_path, "2026-10-05T06:00:00+00:00")[0] as client:
        client.put("/api/profile", json=PROFILE)
        client.put("/api/availability", json={"status": "at_pg"})
        data = client.put("/api/availability", json={"status": None}).json()
        assert data["context"]["availability"] == "at_office"


def test_delivery_window_expires_even_when_saved_flag_is_enabled(tmp_path):
    client, clock = client_at(tmp_path, "2026-10-05T06:00:00+00:00")
    with client:
        client.put("/api/profile", json=PROFILE)
        data = client.put(
            "/api/delivery-mode",
            json={"enabled": True, "expires_at": "2026-10-05T06:30:00+00:00"},
        ).json()
        assert data["context"]["delivery_mode_active"]
        clock["now"] = datetime(2026, 10, 5, 6, 30, tzinfo=UTC)
        data = client.get("/api/dashboard").json()
        assert not data["context"]["delivery_mode_active"]
        assert data["context"]["delivery_mode_expired"]


@pytest.mark.parametrize("expires_at", ["2026-10-05T05:00:00+00:00", "2026-10-05T07:00:00", None])
def test_invalid_delivery_windows_are_rejected(tmp_path, expires_at):
    with client_at(tmp_path, "2026-10-05T06:00:00+00:00")[0] as client:
        client.put("/api/profile", json=PROFILE)
        response = client.put(
            "/api/delivery-mode", json={"enabled": True, "expires_at": expires_at}
        )
        assert response.status_code == 422


@pytest.mark.parametrize(
    "changes",
    [
        {"office_start": "19:00", "office_end": "09:00"},
        {"office_days": [0, 0]},
        {"office_days": [7]},
        {"weekend": "invented_status"},
        {"timezone": "invalid_timezone"},
    ],
)
def test_invalid_preferences_do_not_replace_saved_policy(tmp_path, changes):
    with client_at(tmp_path, "2026-10-05T06:00:00+00:00")[0] as client:
        client.put("/api/profile", json=PROFILE)
        assert client.put("/api/profile", json={**PROFILE, **changes}).status_code == 422
        assert client.get("/api/dashboard").json()["profile"]["office_start"] == "09:00:00"


def test_override_and_delivery_mode_persist_across_restarts(tmp_path):
    with client_at(tmp_path, "2026-10-05T06:00:00+00:00")[0] as client:
        client.put("/api/profile", json=PROFILE)
        client.put("/api/availability", json={"status": "at_pg"})
        client.put(
            "/api/delivery-mode",
            json={"enabled": True, "expires_at": "2026-10-05T07:00:00+00:00"},
        )
    with client_at(tmp_path, "2026-10-05T06:10:00+00:00")[0] as restarted:
        data = restarted.get("/api/dashboard").json()
        assert data["context"]["today_override"] == "at_pg"
        assert data["context"]["delivery_mode_active"]


def test_disabling_delivery_mode_clears_saved_expiry(tmp_path):
    with client_at(tmp_path, "2026-10-05T06:00:00+00:00")[0] as client:
        client.put("/api/profile", json=PROFILE)
        client.put(
            "/api/delivery-mode",
            json={"enabled": True, "expires_at": "2026-10-05T07:00:00+00:00"},
        )
        data = client.put("/api/delivery-mode", json={"enabled": False}).json()
        assert data["delivery_mode"] == {"enabled": False, "expires_at": None}
        assert not data["context"]["delivery_mode_active"]


def test_invalid_window_does_not_replace_an_active_window(tmp_path):
    with client_at(tmp_path, "2026-10-05T06:00:00+00:00")[0] as client:
        client.put("/api/profile", json=PROFILE)
        client.put(
            "/api/delivery-mode",
            json={"enabled": True, "expires_at": "2026-10-05T07:00:00+00:00"},
        )
        response = client.put(
            "/api/delivery-mode",
            json={"enabled": True, "expires_at": "2026-10-05T05:00:00+00:00"},
        )
        assert response.status_code == 422
        assert client.get("/api/dashboard").json()["context"]["delivery_mode_active"]


def test_incomplete_setup_cannot_produce_an_active_delivery_context(tmp_path):
    with client_at(tmp_path, "2026-10-05T06:00:00+00:00")[0] as client:
        client.put("/api/profile", json=PROFILE)
        client.put(
            "/api/delivery-mode",
            json={"enabled": True, "expires_at": "2026-10-05T07:00:00+00:00"},
        )
        data = client.put("/api/profile", json={**PROFILE, "guard_location": "  "}).json()
        assert not data["context"]["delivery_mode_active"]
        assert not data["context"]["setup_complete"]
