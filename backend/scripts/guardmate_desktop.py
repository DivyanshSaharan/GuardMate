"""Frozen desktop entrypoint; native workers dispatch before application imports."""

import multiprocessing
import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guardmate.frozen_runtime import dispatch_worker


def main():
    multiprocessing.freeze_support()
    dispatched = dispatch_worker()
    if dispatched is not None:
        return dispatched

    import argparse
    import ctypes
    import json
    import os
    import secrets
    import socket
    import threading
    import time
    import webbrowser
    from ctypes import wintypes

    parser = argparse.ArgumentParser(description="GuardMate Windows desktop application")
    parser.add_argument("--state-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true", help="Check assets without opening devices")
    arguments = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("The GuardMate desktop application requires Windows.")

    from guardmate.desktop.config import bundle_root, configure_environment, load_session

    paths = configure_environment(state=arguments.state_dir)
    os.environ["GUARDMATE_MODE"] = "desktop"
    session = load_session(paths["root"])
    if session is not None:
        import re

        if re.fullmatch(r"[a-f0-9]{64}", session["account_id"]) is None:
            raise SystemExit("The saved account identity is invalid. Run setup again.")
        paths["data_dir"] = paths["root"] / "accounts" / session["account_id"] / "data"
        paths["data_dir"].mkdir(parents=True, exist_ok=True)
        os.environ["GUARDMATE_DATA_DIR"] = str(paths["data_dir"])
    if arguments.check:
        required = [
            paths["frontend_dir"] / "index.html",
            paths["whisper_cli"],
            paths["models_dir"] / "ggml-base.en.bin",
            paths["piper_model"],
        ]
        print(
            json.dumps(
                {"assets": [{"name": item.name, "ready": item.is_file()} for item in required]}
            )
        )
        return 0 if all(item.is_file() for item in required) else 2

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateMutexW(None, False, "Local\\GuardMateDesktop")
    if not handle:
        raise SystemExit("GuardMate could not create its application lock.")
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        info = paths["root"] / "running.json"
        try:
            port = json.loads(info.read_text(encoding="utf-8"))["port"]
            if type(port) is int and 1024 <= port <= 65535:
                webbrowser.open(f"http://127.0.0.1:{port}/")
        except (OSError, ValueError, KeyError):
            pass
        return 0

    import uvicorn
    from guardmate.desktop.remote import RemoteProvider
    from guardmate.main import create_app
    from guardmate.speech.service import SpeechService

    token, ticket = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    speech = SpeechService(paths["root"])
    application = create_app(
        paths["data_dir"],
        provider=RemoteProvider(paths["root"]),
        speech_service=speech,
        frontend_dir=paths["frontend_dir"],
        desktop_access=(token, ticket),
        desktop_root=bundle_root(),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host="127.0.0.1",
            port=0,
            workers=1,
            log_level="warning",
            access_log=False,
            proxy_headers=False,
        )
    )

    @application.post("/api/desktop/quit")
    def quit_application():
        application.state.desktop_calls.stop()
        server.should_exit = True
        return {"stopping": True}

    # The frontend mount is the final route; insert shutdown before that mount.
    application.router.routes.insert(-1, application.router.routes.pop())
    bound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    bound.bind(("127.0.0.1", 0))
    bound.listen(128)
    port = bound.getsockname()[1]
    info = paths["root"] / "running.json"
    info.write_text(json.dumps({"port": port}), encoding="utf-8")

    def show_when_ready():
        for _ in range(200):
            if server.started:
                webbrowser.open(f"http://127.0.0.1:{port}/desktop/open?ticket={ticket}")
                return
            if server.should_exit:
                return
            time.sleep(0.1)

    if not arguments.no_browser:
        threading.Thread(target=show_when_ready, daemon=True).start()
    try:
        server.run(sockets=[bound])
    finally:
        bound.close()
        info.unlink(missing_ok=True)
        kernel.CloseHandle(handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
