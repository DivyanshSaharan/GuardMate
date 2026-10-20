"""Explicit packaged-worker and headless-server probe; no call audio or paid inference."""

import argparse
import http.client
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    arguments = parser.parse_args()
    if not arguments.run:
        parser.error("Use --run to launch the packaged workers and a private test server.")
    for name in list(os.environ):
        if name.upper().startswith(("MONGODB_", "TINKER_", "GUARDMATE_", "RENDER")):
            os.environ.pop(name, None)
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    from guardmate.cellular.call_audio import reply_audio
    from guardmate.speech.service import SpeechService

    runtime = ROOT / ".cache/windows-build/dist/GuardMate"
    executable = runtime / "GuardMate.exe"
    probe = ROOT / ".cache/windows-runtime-probe" / str(uuid4())
    probe.mkdir(parents=True)
    output = probe / "synthetic.wav"
    piper = ROOT / "models/speech/en_US-ljspeech-high.onnx"
    hidden = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    started = time.monotonic()
    child = subprocess.run(
        [str(executable), "--worker", "speech", str(piper), str(output)],
        input=b"GuardMate is ready for a supervised delivery conversation.",
        capture_output=True, timeout=60, cwd=probe, **hidden,
    )
    if child.returncode != 0 or not output.is_file():
        raise RuntimeError("Packaged speech worker failed.")
    pcm = reply_audio(output.read_bytes())
    speech = SpeechService(
        ROOT, whisper_cli=runtime / "_internal/whisper/whisper-cli.exe",
        whisper_model=ROOT / "models/speech/ggml-base.en.bin",
    )
    transcript = speech.transcribe(pcm)
    environment = dict(os.environ)
    state = probe / "state"
    process = subprocess.Popen(
        [str(executable), "--no-browser", "--state-dir", str(state)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        env=environment, cwd=probe, **hidden,
    )
    try:
        port = None
        for _ in range(150):
            if process.poll() is not None:
                raise RuntimeError("Packaged application exited before becoming ready.")
            info = state / "running.json"
            if info.is_file():
                port = json.loads(info.read_text(encoding="utf-8"))["port"]
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                    connection.request("GET", "/api/health")
                    response = connection.getresponse()
                    if response.status == 200:
                        response.read()
                        connection.close()
                        break
                except (OSError, http.client.HTTPException):
                    pass
            time.sleep(0.1)
        else:
            raise RuntimeError("Packaged server did not become ready.")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request("GET", "/api/dashboard")
        response = connection.getresponse()
        if response.status != 401:
            raise RuntimeError("Packaged resident data was not protected.")
        response.read()
        connection.close()
        result = {
            "packaged_speech_worker": True, "packaged_whisper_cli": True,
            "headless_runtime_health": True, "unauthenticated_resident_api": 401,
            "synthetic_transcript": transcript["text"],
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "phone_streams_opened": False, "hosted_inference_sent": False,
        }
        (probe / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)


if __name__ == "__main__":
    main()
