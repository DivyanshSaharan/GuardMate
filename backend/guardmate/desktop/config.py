"""Private desktop paths and Windows CurrentUser DPAPI session storage."""

import ctypes
import json
import os
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlsplit

MAX_SESSION_BYTES = 32_768


class DesktopConfigError(RuntimeError):
    pass


def user_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise DesktopConfigError("Windows local application data is unavailable.")
    return Path(base).resolve() / "GuardMate"


def bundle_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3])).resolve()


def configure_environment(
    *, bundle: Path | None = None, state: Path | None = None
) -> dict[str, Path]:
    """Prepare local state without inheriting the publisher's production settings."""
    release = (bundle or bundle_root()).resolve()
    root = (state or user_root()).resolve()
    for name in list(os.environ):
        upper = name.upper()
        if upper.startswith(
            (
                "MONGODB_",
                "TINKER_",
                "RENDER",
                "GUARDMATE_ACCESS_",
                "GUARDMATE_GOOGLE_",
                "GUARDMATE_R2_",
            )
        ) or upper in {
            "GUARDMATE_SAMPLER_CHECKPOINT",
            "GUARDMATE_BACKEND_URL",
            "GUARDMATE_MODE",
        }:
            os.environ.pop(name, None)
    models = root / "models" / "speech"
    data = root / "data"
    for directory in (root, models, data, root / "cache"):
        directory.mkdir(parents=True, exist_ok=True)
    bundled_models = release / "models" / "speech"
    selected = models if (models / "ggml-base.en.bin").is_file() else bundled_models
    paths = {
        "root": root,
        "data_dir": data,
        "frontend_dir": release / "dist",
        "models_dir": selected,
        "whisper_cli": release / "whisper" / "whisper-cli.exe",
        "piper_model": selected / "en_US-ljspeech-high.onnx",
    }
    os.environ.update(
        {
            "PYTHON_DOTENV_DISABLED": "1",
            "GUARDMATE_DATA_DIR": str(data),
            "GUARDMATE_WHISPER_CLI": str(paths["whisper_cli"]),
            "GUARDMATE_WHISPER_MODEL": str(selected / "ggml-base.en.bin"),
            "GUARDMATE_PIPER_MODEL": str(paths["piper_model"]),
            "HF_HOME": str(root / "cache" / "huggingface"),
        }
    )
    return paths


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _protect(value: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise DesktopConfigError("Desktop credentials require Windows DPAPI.")
    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    source, output = _Blob(len(value), buffer), _Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise DesktopConfigError("Windows could not unlock the desktop session.")
    try:
        if output.size > MAX_SESSION_BYTES:
            raise DesktopConfigError("The desktop session exceeds its size limit.")
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def validate_session(session: dict) -> dict:
    if not isinstance(session, dict) or session.get("version") != 1:
        raise DesktopConfigError("Unsupported desktop session.")
    for key in ("server_url", "bootstrap_token", "access_token", "account_id"):
        if not isinstance(session.get(key), str) or not 1 <= len(session[key]) <= 4096:
            raise DesktopConfigError("The desktop session is incomplete.")
    origin = urlsplit(session["server_url"])
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
        or origin.path not in ("", "/")
    ):
        raise DesktopConfigError("Use the configured HTTPS GuardMate service.")
    if type(session.get("expires_at")) not in (int, float):
        raise DesktopConfigError("The desktop session expiration is missing.")
    return session


def load_session(root: Path | None = None) -> dict | None:
    path = (root or user_root()) / "session.dat"
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_SESSION_BYTES:
            raise DesktopConfigError("The desktop session exceeds its size limit.")
        return validate_session(json.loads(_protect(path.read_bytes(), decrypt=True)))
    except (OSError, ValueError, UnicodeError) as error:
        raise DesktopConfigError("The desktop session cannot be read. Run setup again.") from error


def save_session(session: dict, root: Path | None = None) -> None:
    encoded = json.dumps(validate_session(session), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_SESSION_BYTES:
        raise DesktopConfigError("The desktop session exceeds its size limit.")
    protected = _protect(encoded)
    directory = root or user_root()
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix="session-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(protected)
        temporary.replace(directory / "session.dat")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
