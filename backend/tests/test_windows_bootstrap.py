"""Actual unsigned bootstrap/DPAPI protocol checks; no GUI, network, audio or account APIs."""

import hashlib
import importlib.util
import json
import os
import stat
import struct
import subprocess
import zipfile
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


@pytest.fixture(scope="module")
def bootstrap_methods(template, tmp_path_factory):
    """Call actual private installer methods without entering its GUI/activation path."""
    folder = tmp_path_factory.mktemp("bootstrap-methods")
    assembly, source, harness = (
        folder / "template.exe",
        folder / "Harness.cs",
        folder / "Harness.exe",
    )
    assembly.write_bytes(template)
    source.write_text(
        """
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Web.Script.Serialization;
class Harness {
    static int Main(string[] args) {
        try {
            var type = Assembly.LoadFile(args[0]).GetType("Bootstrap", true);
            if (args[1] == "extract") {
                type.GetMethod("Extract", BindingFlags.NonPublic | BindingFlags.Static)
                    .Invoke(null, new object[] { args[2], args[3] });
                return 0;
            }
            var serializer = new JavaScriptSerializer();
            var raw = serializer.Deserialize<Dictionary<string, object>>(File.ReadAllText(args[2]));
            var assets = (List<Dictionary<string, object>>)type
                .GetMethod("ReadSpeechAssets", BindingFlags.NonPublic | BindingFlags.Static)
                .Invoke(null, new object[] { args[2] });
            Console.WriteLine(serializer.Serialize(new {
                raw_array_type = raw["assets"].GetType().FullName,
                count = assets.Count,
                paths = assets.Select(asset => (string)asset["path"]).ToArray(),
                sizes = assets.Select(asset => Convert.ToInt64(asset["size"])).ToArray()
            }));
            return 0;
        } catch (TargetInvocationException error) {
            Console.WriteLine(error.InnerException.GetType().FullName);
            return 2;
        }
    }
}
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            str(COMPILER),
            "/nologo",
            "/target:exe",
            "/platform:x64",
            f"/out:{harness}",
            "/reference:System.Web.Extensions.dll",
            str(source),
        ],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout.decode(errors="replace")

    def invoke(method, *arguments):
        return subprocess.run(
            [str(harness), str(assembly), method, *map(str, arguments)],
            capture_output=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    return invoke


@pytest.mark.parametrize("separator", ["/", "\\"])
def test_actual_bootstrap_extracts_windows_and_portable_zip_names(
    bootstrap_methods, tmp_path, separator
):
    archive, destination = tmp_path / "runtime.zip", tmp_path / "extracted"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("GuardMate.exe", b"offline-runtime")
        stream.writestr(separator.join(("_internal", "assets", "")), b"")
        stream.writestr(separator.join(("_internal", "assets", "manifest.json")), b"{}")
    result = bootstrap_methods("extract", archive, destination)
    assert result.returncode == 0, result.stdout.decode(errors="replace")
    assert (destination / "GuardMate.exe").read_bytes() == b"offline-runtime"
    assert (destination / "_internal/assets/manifest.json").read_bytes() == b"{}"


@pytest.mark.parametrize(
    "kind",
    [
        "parent-forward",
        "parent-backslash",
        "nested-forward",
        "nested-backslash",
        "absolute",
        "drive",
        "symlink",
    ],
)
def test_actual_bootstrap_rejects_unsafe_zip_entries_without_outside_writes(
    bootstrap_methods, tmp_path, kind
):
    archive, destination = tmp_path / "unsafe.zip", tmp_path / "extracted"
    outside = tmp_path / "outside.txt"
    names = {
        "parent-forward": "../outside.txt",
        "parent-backslash": "..\\outside.txt",
        "nested-forward": "folder/../../outside.txt",
        "nested-backslash": "folder\\..\\..\\outside.txt",
        "absolute": outside.as_posix().split(":", 1)[1],
        "drive": str(outside),
        "symlink": "link",
    }
    entry = zipfile.ZipInfo(names[kind])
    if kind == "symlink":
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr(entry, b"../outside.txt" if kind == "symlink" else b"unsafe")
    result = bootstrap_methods("extract", archive, destination)
    assert result.returncode == 2
    assert result.stdout.strip() == b"System.IO.InvalidDataException"
    assert not outside.exists()
    assert not any(destination.rglob("*"))


def test_actual_framework_arraylist_manifest_retains_three_speech_assets(
    bootstrap_methods, tmp_path
):
    assets = [
        {"path": "models/speech/ggml-base.en.bin", "size": 147964211},
        {"path": "models/speech/en_US-ljspeech-high.onnx", "size": 114199011},
        {"path": "models/speech/en_US-ljspeech-high.onnx.json", "size": 4970},
    ]
    manifest = tmp_path / "speech-assets.json"
    manifest.write_text(json.dumps({"assets": assets}), encoding="utf-8")
    result = bootstrap_methods("manifest", manifest)
    assert result.returncode == 0, result.stdout.decode(errors="replace")
    parsed = json.loads(result.stdout)
    assert parsed["raw_array_type"] == "System.Collections.ArrayList"
    assert parsed["count"] == 3
    assert parsed["paths"] == [asset["path"] for asset in assets]
    assert parsed["sizes"] == [asset["size"] for asset in assets]


@pytest.mark.parametrize("assets", [[], [None], [{}] * 9])
def test_actual_bootstrap_rejects_invalid_or_unbounded_speech_manifest(
    bootstrap_methods, tmp_path, assets
):
    manifest = tmp_path / "speech-assets.json"
    manifest.write_text(json.dumps({"assets": assets}), encoding="utf-8")
    result = bootstrap_methods("manifest", manifest)
    assert result.returncode == 2
    assert result.stdout.strip() == b"System.IO.InvalidDataException"


def test_release_archive_uses_portable_names_and_is_immutable(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "windows_create_archive", ROOT / "packaging/windows/create_archive.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runtime = tmp_path / "runtime"
    nested = runtime / "_internal/assets"
    nested.mkdir(parents=True)
    (runtime / "GuardMate.exe").write_bytes(b"offline-runtime")
    (nested / "manifest.json").write_bytes(b"{}")
    output = tmp_path / "runtime.zip"
    module.create_archive(runtime, output)
    with zipfile.ZipFile(output) as archive:
        assert "_internal/assets/manifest.json" in archive.namelist()
        assert all("\\" not in entry.orig_filename for entry in archive.infolist())
        assert archive.read("_internal/assets/manifest.json") == b"{}"
    digest = hashlib.sha256(output.read_bytes()).digest()
    with pytest.raises(FileExistsError):
        module.create_archive(runtime, output)
    assert hashlib.sha256(output.read_bytes()).digest() == digest
