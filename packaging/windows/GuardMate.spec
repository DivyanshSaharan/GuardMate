# PyInstaller spec: installed dependencies and explicitly staged public resources only.
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

root = Path(SPECPATH).resolve().parents[1]
stage = root / ".cache/windows-build/assets"
datas = [(str(root / "dist"), "dist"),
         (str(stage / "speech-assets.json"), "."),
         (str(stage / "licenses"), "licenses")]
binaries = [(str(path), "whisper") for path in (stage / "whisper").iterdir() if path.is_file()]
hiddenimports = ["guardmate.speech.worker", "guardmate.cellular.call_audio_worker",
                 "guardmate.cellular.utterance_worker", "guardmate.desktop.audio_worker",
                 "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
                 "uvicorn.protocols.http.h11_impl", "uvicorn.lifespan.on", "_cffi_backend"]
for package in ("piper", "onnxruntime", "sounddevice", "_sounddevice_data", "cffi"):
    more_data, more_binaries, more_imports = collect_all(package)
    datas += more_data
    binaries += more_binaries
    hiddenimports += [name for name in more_imports if not name.startswith(
        ("piper.train", "piper.http_server", "onnxruntime.tools", "onnxruntime.quantization",
         "onnxruntime.datasets"))]
hiddenimports += collect_submodules("pymongo")
for package in ("piper-tts", "onnxruntime", "sounddevice", "cffi", "httpx", "pymongo"):
    datas += copy_metadata(package)

a = Analysis([str(root / "backend/scripts/guardmate_desktop.py")],
             pathex=[str(root / "backend")], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, hookspath=[], runtime_hooks=[],
             excludes=["torch", "tensorflow", "transformers", "tinker", "pytest", "IPython"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="GuardMate",
          icon=str(root / "packaging/windows/assets/guardmate.ico"),
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True, hide_console="hide-early", disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="GuardMate")
