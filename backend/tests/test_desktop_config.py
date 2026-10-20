"""Local desktop state tests; no application import or real account credentials."""

import json

import pytest
from guardmate.desktop import config


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    for name in (
        "MONGODB_URI",
        "TINKER_API_KEY",
        "GUARDMATE_ACCESS_USERNAME",
        "GUARDMATE_ACCESS_PASSWORD",
        "RENDER",
    ):
        monkeypatch.delenv(name, raising=False)


def session():
    return {
        "version": 1,
        "server_url": "https://example.invalid",
        "bootstrap_token": "fake-bootstrap",
        "access_token": "fake-access",
        "account_id": "fake-account",
        "expires_at": 1234567,
    }


def test_desktop_overrides_inherited_production_environment(tmp_path, monkeypatch):
    for key in (
        "MONGODB_URI",
        "TINKER_API_KEY",
        "GUARDMATE_ACCESS_PASSWORD",
        "RENDER_EXTERNAL_URL",
    ):
        monkeypatch.setenv(key, "not-a-real-secret")
    bundle, state = tmp_path / "bundle", tmp_path / "private"
    paths = config.configure_environment(bundle=bundle, state=state)
    assert paths["data_dir"] == state / "data"
    assert paths["frontend_dir"] == bundle / "dist"
    assert paths["whisper_cli"] == bundle / "whisper/whisper-cli.exe"
    assert config.os.environ["PYTHON_DOTENV_DISABLED"] == "1"
    assert config.os.environ["GUARDMATE_DATA_DIR"] == str(state / "data")
    for key in (
        "MONGODB_URI",
        "TINKER_API_KEY",
        "GUARDMATE_ACCESS_PASSWORD",
        "RENDER_EXTERNAL_URL",
    ):
        assert key not in config.os.environ


def test_session_file_contains_only_protected_bytes_and_rejects_tampering(tmp_path, monkeypatch):
    value = session()
    encoded = json.dumps(value, allow_nan=False).encode()

    def protect(body, *, decrypt=False):
        if decrypt:
            if body != b"fake-DPAPI-opaque-blob":
                raise config.DesktopConfigError("Invalid protected blob")
            return encoded
        assert json.loads(body) == value
        return b"fake-DPAPI-opaque-blob"

    monkeypatch.setattr(config, "_protect", protect)
    config.save_session(value, tmp_path)
    assert (tmp_path / "session.dat").read_bytes() == b"fake-DPAPI-opaque-blob"
    assert config.load_session(tmp_path) == value
    (tmp_path / "session.dat").write_bytes(b"tampered")
    with pytest.raises(config.DesktopConfigError):
        config.load_session(tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid",
        "https://user:password@example.invalid",
        "https://example.invalid/?token=private",
        "file:///tmp/session",
    ],
)
def test_session_requires_https_service_origin(url):
    value = session()
    value["server_url"] = url
    with pytest.raises(config.DesktopConfigError):
        config.validate_session(value)
