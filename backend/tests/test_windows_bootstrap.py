"""Actual unsigned bootstrap/DPAPI protocol checks; no GUI, network, audio or account APIs."""

import hashlib
import json
import os
import struct
import subprocess
from pathlib import Path

import pytest
from guardmate.desktop.config import load_session

ROOT = Path(__file__).resolve().parents[2]
COMPILER = (
    Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
)
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not COMPILER.is_file(), reason="Windows .NET only"
)


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    folder = tmp_path_factory.mktemp("unsigned-bootstrap")
    target = folder / "template.exe"
    references = [
        "System.Windows.Forms",
        "System.Drawing",
        "System.Web.Extensions",
        "System.Security",
        "System.IO.Compression",
        "System.IO.Compression.FileSystem",
        "Microsoft.CSharp",
    ]
    result = subprocess.run(
        [
            str(COMPILER),
            "/nologo",
            "/target:winexe",
            "/platform:x64",
            f"/out:{target}",
            *(f"/reference:{name}.dll" for name in references),
            str(ROOT / "packaging/windows/bootstrap.cs"),
        ],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, "The unsigned Windows bootstrap did not compile."
    return target.read_bytes()


def personalized(template, **updates):
    envelope = {
        "version": 1,
        "server_url": "https://guardmate.onrender.com",
        "bootstrap_token": "fake-offline-account-A",
        **updates,
    }
    body = json.dumps(envelope).encode()
    return (
        template
        + body
        + hashlib.sha256(body).digest()
        + struct.pack("<I", len(body))
        + b"GMATE-ENVELOPE1!"
    )


def run(executable, *args):
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("TINKER_", "MONGODB_", "RENDER", "GUARDMATE_ACCESS_"))
    }
    environment["PYTHON_DOTENV_DISABLED"] = "1"
    return subprocess.run(
        [str(executable), *map(str, args)],
        env=environment,
        capture_output=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).returncode


def test_actual_executable_and_copy_keep_account_envelope_private_in_dpapi(template, tmp_path):
    executable, copied = tmp_path / "Setup.exe", tmp_path / "CopiedSetup.exe"
    executable.write_bytes(personalized(template))
    copied.write_bytes(executable.read_bytes())
    values = []
    for index, path in enumerate((executable, copied)):
        assert run(path, "--verify-envelope") == 0
        private = tmp_path / f"private-{index}"
        private.mkdir()
        assert run(path, "--probe-envelope", private / "session.dat") == 0
        assert b"fake-offline-account-A" not in (private / "session.dat").read_bytes()
        values.append(load_session(private))
    assert values[0] == values[1]
    assert values[0]["bootstrap_token"] == "fake-offline-account-A"


@pytest.mark.parametrize("kind", ["missing", "corrupt", "oversized", "other-origin", "version"])
def test_actual_bootstrap_rejects_invalid_frames_without_gui_or_network(template, tmp_path, kind):
    content = personalized(template)
    if kind == "missing":
        content = template
    elif kind == "corrupt":
        data = bytearray(content)
        data[-53] ^= 1
        content = data
    elif kind == "oversized":
        content = personalized(template, bootstrap_token="x" * 17000)
    elif kind == "other-origin":
        content = personalized(template, server_url="https://example.invalid")
    else:
        content = personalized(template, version=2)
    executable = tmp_path / "InvalidSetup.exe"
    executable.write_bytes(content)
    assert run(executable, "--verify-envelope") == 2
