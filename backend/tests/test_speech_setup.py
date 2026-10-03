"""Offline pin/extraction checks; fake downloads never contact upstream services."""

import hashlib
import io
import stat
import struct
import wave
import zipfile
from array import array

import pytest
from scripts import setup_speech as setup
from scripts import smoke_speech as smoke


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Speech setup tests must not use the network")

    monkeypatch.setattr(setup.urllib.request, "urlopen", denied)


def asset(content, path="models/speech/test.bin"):
    return setup.Asset(
        path=path,
        url="https://invalid.example.test/offline-fixture",
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
    )


def fake_download(monkeypatch, content):
    requests = []

    def open_response(request, timeout):
        requests.append((request, timeout))
        return io.BytesIO(content)

    monkeypatch.setattr(setup.urllib.request, "urlopen", open_response)
    return requests


def pinned_zip(tmp_path, monkeypatch, entries, *, oversized=False):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)
    content = buffer.getvalue()
    if oversized:
        # Only modify central-directory metadata, not allocate a 100 MB fixture.
        data = bytearray(content)
        directory = data.index(b"PK\x01\x02")
        struct.pack_into("<I", data, directory + 24, 100_000_001)
        content = bytes(data)
    fixture = asset(content, ".cache/voice/downloads/fixture.zip")
    monkeypatch.setattr(setup, "WINDOWS_ZIP", fixture)
    path = tmp_path / fixture.path
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return path, fixture


def test_existing_correct_asset_is_verified_and_reused_without_network(tmp_path):
    content = b"pinned local model fixture"
    fixture = asset(content)
    target = tmp_path / fixture.path
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    before = target.stat().st_mtime_ns

    assert setup.verified(target, fixture)
    setup.acquire(fixture, tmp_path)

    assert target.read_bytes() == content
    assert target.stat().st_mtime_ns == before


@pytest.mark.parametrize("existing", [b"different same length", b"short"])
def test_existing_wrong_asset_refuses_overwrite(tmp_path, existing):
    fixture = asset(b"correct same length!!")
    target = tmp_path / fixture.path
    target.parent.mkdir(parents=True)
    target.write_bytes(existing)

    assert not setup.verified(target, fixture)
    with pytest.raises(ValueError, match="refusing overwrite"):
        setup.acquire(fixture, tmp_path)

    assert target.read_bytes() == existing


def test_download_is_verified_before_installation(tmp_path, monkeypatch):
    content = b"correct fixture"
    fixture = asset(content)
    requests = fake_download(monkeypatch, content)

    setup.acquire(fixture, tmp_path)

    assert setup.verified(tmp_path / fixture.path, fixture)
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url == fixture.url
    assert request.get_header("User-agent") == "GuardMate-local-speech-setup"
    assert timeout == 30
    assert list((tmp_path / fixture.path).parent.iterdir()) == [tmp_path / fixture.path]


@pytest.mark.parametrize("downloaded", [b"incorrect bytes", b"short"])
def test_wrong_download_hash_or_size_is_not_installed(tmp_path, monkeypatch, downloaded):
    fixture = asset(b"correct fixture")
    fake_download(monkeypatch, downloaded)

    with pytest.raises(ValueError, match="failed SHA-256/size verification"):
        setup.acquire(fixture, tmp_path)

    assert not (tmp_path / fixture.path).exists()
    assert not list((tmp_path / fixture.path).parent.iterdir())


def test_oversized_download_is_refused_and_temporary_file_is_cleaned(tmp_path, monkeypatch):
    fixture = asset(b"correct fixture")
    fake_download(monkeypatch, b"correct fixture plus unpinned extra data")

    with pytest.raises(ValueError, match="exceeded its pinned bounds"):
        setup.acquire(fixture, tmp_path)

    assert not (tmp_path / fixture.path).exists()
    assert not list((tmp_path / fixture.path).parent.iterdir())


def test_download_time_bound_is_enforced(tmp_path, monkeypatch):
    fixture = asset(b"correct fixture")
    fake_download(monkeypatch, b"correct fixture")
    times = iter([0, 301])
    monkeypatch.setattr(setup.time, "monotonic", lambda: next(times))

    with pytest.raises(ValueError, match="exceeded its pinned bounds"):
        setup.acquire(fixture, tmp_path)

    assert not (tmp_path / fixture.path).exists()
    assert not list((tmp_path / fixture.path).parent.iterdir())


def test_competing_asset_is_never_overwritten(tmp_path, monkeypatch):
    fixture = asset(b"correct fixture")
    target = tmp_path / fixture.path

    def raced(request, timeout):
        target.write_bytes(b"created by someone else")
        return io.BytesIO(b"correct fixture")

    monkeypatch.setattr(setup.urllib.request, "urlopen", raced)

    with pytest.raises(FileExistsError):
        setup.acquire(fixture, tmp_path)

    assert target.read_bytes() == b"created by someone else"


def test_extracts_and_reuses_a_verified_runtime_without_replacing_files(tmp_path, monkeypatch):
    pinned_zip(
        tmp_path,
        monkeypatch,
        [
            ("Release/", b""),
            ("Release/whisper-cli.exe", b"fake cli"),
            ("Release/whisper.dll", b"fake library"),
        ],
    )
    runtime = tmp_path / ".cache/voice/whisper-v1.8.2/Release"

    setup.extract_windows(tmp_path)
    assert (runtime / "whisper-cli.exe").read_bytes() == b"fake cli"
    assert (runtime / "whisper.dll").read_bytes() == b"fake library"
    before = (runtime / "whisper-cli.exe").stat().st_mtime_ns

    def no_copy(*args, **kwargs):
        raise AssertionError("Existing verified runtime files must not be rewritten")

    monkeypatch.setattr(setup.shutil, "copyfileobj", no_copy)
    setup.extract_windows(tmp_path)
    assert (runtime / "whisper-cli.exe").stat().st_mtime_ns == before


def test_archive_checksum_pin_is_required_before_extraction(tmp_path, monkeypatch):
    path, fixture = pinned_zip(tmp_path, monkeypatch, [("Release/whisper-cli.exe", b"fake cli")])
    # Same archive byte count, but not the bytes specified by the pin.
    altered = bytearray(path.read_bytes())
    altered[0] ^= 1
    path.write_bytes(altered)

    assert not setup.verified(path, fixture)
    with pytest.raises(ValueError, match="pinned checksum"):
        setup.extract_windows(tmp_path)

    assert not (tmp_path / ".cache/voice/whisper-v1.8.2").exists()


@pytest.mark.parametrize("name", ["../escaped.bin", "../../escaped.bin", "/escaped.bin"])
def test_extraction_refuses_path_traversal(tmp_path, monkeypatch, name):
    pinned_zip(tmp_path, monkeypatch, [(name, b"not safe")])

    with pytest.raises(ValueError, match="Unsafe archive path"):
        setup.extract_windows(tmp_path)

    destination = tmp_path / ".cache/voice/whisper-v1.8.2"
    assert not list(destination.rglob("*"))


def test_extraction_refuses_symlinks(tmp_path, monkeypatch):
    link = zipfile.ZipInfo("Release/whisper-cli.exe")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    pinned_zip(tmp_path, monkeypatch, [(link, b"../../outside")])

    with pytest.raises(ValueError, match="links are not permitted"):
        setup.extract_windows(tmp_path)

    assert not list((tmp_path / ".cache/voice/whisper-v1.8.2").rglob("*"))


def test_extraction_refuses_oversized_archive_before_writing_runtime(tmp_path, monkeypatch):
    pinned_zip(tmp_path, monkeypatch, [("Release/whisper-cli.exe", b"fake cli")], oversized=True)

    with pytest.raises(ValueError, match="exceeds the extraction bound"):
        setup.extract_windows(tmp_path)

    assert not list((tmp_path / ".cache/voice/whisper-v1.8.2").rglob("*"))


def test_modified_existing_runtime_is_preserved_and_refused(tmp_path, monkeypatch):
    pinned_zip(tmp_path, monkeypatch, [("Release/whisper-cli.exe", b"fake cli")])
    runtime = tmp_path / ".cache/voice/whisper-v1.8.2/Release/whisper-cli.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"user modified runtime")

    with pytest.raises(ValueError, match="differs; refusing overwrite"):
        setup.extract_windows(tmp_path)

    assert runtime.read_bytes() == b"user modified runtime"


@pytest.mark.parametrize("rate", [16000, 22050, 44100, 48000])
def test_smoke_conversion_produces_pcm16_mono_16k_with_same_duration(rate):
    source = io.BytesIO()
    samples = array("h", [index % 1000 - 500 for index in range(rate // 10)])
    with wave.open(source, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(rate)
        recording.writeframes(samples.tobytes())

    converted = smoke.pcm16_16k(source.getvalue())

    assert converted[:4] == b"RIFF"
    assert converted[8:12] == b"WAVE"
    assert struct.unpack_from("<I", converted, 4)[0] + 8 == len(converted)
    with wave.open(io.BytesIO(converted), "rb") as result:
        assert result.getnchannels() == 1
        assert result.getsampwidth() == 2
        assert result.getframerate() == 16000
        assert result.getnframes() == 1600
        assert len(result.readframes(1600)) == 3200


@pytest.mark.parametrize("channels,width", [(2, 2), (1, 1)])
def test_smoke_conversion_refuses_non_mono_pcm16(channels, width):
    source = io.BytesIO()
    with wave.open(source, "wb") as recording:
        recording.setnchannels(channels)
        recording.setsampwidth(width)
        recording.setframerate(22050)
        recording.writeframes(b"\x00" * channels * width * 2205)

    with pytest.raises(ValueError, match="mono PCM16"):
        smoke.pcm16_16k(source.getvalue())
