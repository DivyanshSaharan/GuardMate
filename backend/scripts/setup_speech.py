"""Inspect pinned local speech assets; downloads require explicit --download. No model API calls."""

import argparse
import hashlib
import json
import platform
import shutil
import stat
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WHISPER_REV = "5359861c739e955e79d9a303bcbc70fb988958b1"
VOICE_REV = "c10ece1aade47bb51c153c893d14e5bf8e5b7117"
VOICE_BASE = (
    f"https://huggingface.co/rhasspy/piper-voices/resolve/{VOICE_REV}/en/en_US/ljspeech/high"
)


@dataclass(frozen=True)
class Asset:
    path: str
    url: str
    sha256: str
    size: int


ASSETS = (
    Asset(
        "models/speech/ggml-base.en.bin",
        f"https://huggingface.co/ggerganov/whisper.cpp/resolve/{WHISPER_REV}/ggml-base.en.bin",
        "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002",
        147_964_211,
    ),
    Asset(
        "models/speech/en_US-ljspeech-high.onnx",
        f"{VOICE_BASE}/en_US-ljspeech-high.onnx",
        "5d4f08ba6a2a48c44592eed3ce56bf85e9de3dd4e20df90541ae68a8310c029a",
        114_199_011,
    ),
    Asset(
        "models/speech/en_US-ljspeech-high.onnx.json",
        f"{VOICE_BASE}/en_US-ljspeech-high.onnx.json",
        "7e1f4634af596d83cca997fb7a931ba80b70f8a316a2655ee69c55365e0ace14",
        4_970,
    ),
    Asset(
        "models/speech/LJSPEECH_MODEL_CARD",
        f"{VOICE_BASE}/MODEL_CARD",
        "289f7421072d689a3d91f6c632f486af46f9b8b04417536104a4ea667b8a394b",
        513,
    ),
    Asset(
        "models/speech/PIPER_VOICES_README.md",
        f"https://huggingface.co/rhasspy/piper-voices/resolve/{VOICE_REV}/README.md",
        "33e93643fce5180886300f9f0070eeeab8af148efb8e84361f8904def80fd9fb",
        497,
    ),
    Asset(
        ".cache/voice/licenses/WHISPER_LICENSE",
        "https://raw.githubusercontent.com/ggml-org/whisper.cpp/"
        "4979e04f5dcaccb36057e059bbaed8a2f5288315/LICENSE",
        "e562a2ddfaf8280537795ac5ecd34e3012b6582a147ef69ba6a6a5c08c84757d",
        1_078,
    ),
    Asset(
        ".cache/voice/licenses/PIPER_COPYING",
        "https://raw.githubusercontent.com/OHF-Voice/piper1-gpl/"
        "639388b6317fc4731e91d53da42aea68fd4166ff/COPYING",
        "0ae0485a5bd37a63e63603596417e4eb0e653334fa6c7f932ca3a0e85d4af227",
        35_148,
    ),
)
WINDOWS_ZIP = Asset(
    ".cache/voice/downloads/whisper-bin-x64-v1.8.2.zip",
    "https://github.com/ggml-org/whisper.cpp/releases/download/v1.8.2/whisper-bin-x64.zip",
    "b1514ebc099765e39fa37eb780b92a140a94c86bb0b3b3d98226b38825979732",
    3_832_432,
)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(512 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def verified(path: Path, asset: Asset) -> bool:
    return path.is_file() and path.stat().st_size == asset.size and digest(path) == asset.sha256


def acquire(asset: Asset, root: Path) -> None:
    target = root / asset.path
    if target.exists():
        if not verified(target, asset):
            raise ValueError(
                f"Existing asset does not match its pin; refusing overwrite: {asset.path}"
            )
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        asset.url, headers={"User-Agent": "GuardMate-local-speech-setup"}
    )
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="speech-download-", dir=target.parent) as folder:
        downloaded = Path(folder) / "asset"
        total = 0
        with (
            urllib.request.urlopen(request, timeout=30) as response,
            downloaded.open("wb") as output,
        ):
            for chunk in iter(lambda: response.read(512 * 1024), b""):
                total += len(chunk)
                if total > asset.size or time.monotonic() - started > 300:
                    raise ValueError(f"Download exceeded its pinned bounds: {asset.path}")
                output.write(chunk)
        if not verified(downloaded, asset):
            raise ValueError(f"Downloaded asset failed SHA-256/size verification: {asset.path}")
        # Target was checked above; never replace a user-created or competing file.
        with target.open("xb") as output, downloaded.open("rb") as source:
            shutil.copyfileobj(source, output)


def extract_windows(root: Path) -> None:
    archive_path = root / WINDOWS_ZIP.path
    if not verified(archive_path, WINDOWS_ZIP):
        raise ValueError("Whisper archive must pass its pinned checksum before extraction.")
    destination = root / ".cache/voice/whisper-v1.8.2"
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if sum(member.file_size for member in members) > 100_000_000:
            raise ValueError("Whisper archive exceeds the extraction bound.")
        for member in members:
            target = destination / member.filename
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("Unsafe archive path.")
            if stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError("Archive links are not permitted.")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                with archive.open(member) as source:
                    expected = hashlib.file_digest(source, "sha256").hexdigest()
                if digest(target) != expected:
                    raise ValueError("Existing extracted runtime differs; refusing overwrite.")
                continue
            with archive.open(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--download", action="store_true", help="Download verified open speech assets."
    )
    parser.add_argument(
        "--models-only", action="store_true", help="Skip the Windows whisper.cpp ZIP."
    )
    args = parser.parse_args()
    windows_cpu = platform.system() == "Windows" and platform.machine().lower() in (
        "amd64",
        "x86_64",
    )
    assets = (*ASSETS, WINDOWS_ZIP) if windows_cpu and not args.models_only else ASSETS
    if args.download:
        try:
            for asset in assets:
                print(f"Checking/downloading {asset.path}", flush=True)
                acquire(asset, ROOT)
            if windows_cpu and not args.models_only:
                extract_windows(ROOT)
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            print(
                f"Local speech setup stopped ({type(error).__name__}). "
                "Existing files were preserved."
            )
            return 1
    results = [
        {
            "path": asset.path,
            "sha256": asset.sha256,
            "source": asset.url,
            "verified": verified(ROOT / asset.path, asset),
        }
        for asset in assets
    ]
    print(
        json.dumps(
            {"assets": results, "network_requested": args.download, "model_api_requests": 0},
            indent=2,
        )
    )
    if not windows_cpu or args.models_only:
        print("Build/configure whisper.cpp separately on this platform; see docs/browser-voice.md.")
    return 0 if all(row["verified"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
