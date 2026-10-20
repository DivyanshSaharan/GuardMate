"""Stage only pinned public speech files and a bounded first-install model manifest."""

import importlib.util
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def setup_module():
    spec = importlib.util.spec_from_file_location(
        "guardmate_speech_pins", ROOT / "backend/scripts/setup_speech.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def prepare(stage: Path) -> dict:
    pins = setup_module()
    archive_path = ROOT / pins.WINDOWS_ZIP.path
    if not pins.verified(archive_path, pins.WINDOWS_ZIP):
        raise ValueError("Run the pinned setup_speech.py --download before packaging.")
    stage.mkdir(parents=True, exist_ok=True)
    manifest = {"version": 1, "assets": []}
    for asset in pins.ASSETS:
        source = ROOT / asset.path
        if not pins.verified(source, asset):
            raise ValueError("A public speech asset failed its pinned checksum.")
        if asset.path.endswith((".bin", ".onnx", ".onnx.json")):
            manifest["assets"].append(
                {
                    "path": asset.path,
                    "url": asset.url,
                    "sha256": asset.sha256,
                    "size": asset.size,
                }
            )
        else:
            target = stage / "licenses/speech" / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    with zipfile.ZipFile(archive_path) as archive:
        selected = [
            member
            for member in archive.infolist()
            if member.filename.startswith("Release/")
            and (member.filename.endswith(".dll") or member.filename == "Release/whisper-cli.exe")
        ]
        if not any(member.filename == "Release/whisper-cli.exe" for member in selected):
            raise ValueError("The pinned Windows speech archive is incomplete.")
        for member in selected:
            if member.file_size > 30_000_000 or Path(member.filename).name != member.filename[8:]:
                raise ValueError("Unexpected native speech archive member.")
            target = stage / "whisper" / Path(member.filename).name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    (stage / "speech-assets.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    prepare(ROOT / ".cache/windows-build/assets")
    print("Verified public speech resources staged; no user configuration or data copied.")
