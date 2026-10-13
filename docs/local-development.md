# Local development

Use Python 3.13+ and Node.js 22.12+. Development and the cellular call route have
been tested on Windows. Run these commands from the repository root.

## Install

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r backend\requirements-dev.txt
.venv\Scripts\python -m pip install -r backend\requirements-ai.txt
npm ci
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Add `TINKER_API_KEY` to the ignored `.env` file for hosted Qwen inference. Text
conversations use inference credit; offline tests do not. Keep credentials out
of frontend code and Git.

To use MongoDB Atlas locally, set `MONGODB_URI` to your cluster's driver URI and
`MONGODB_DATABASE=guardmate`. The local backend uses SQLite in
`.data/guardmate.sqlite3` when no MongoDB URI is configured. Using the deployed
database's URI shares its preferences, conversations and inference budget with
the hosted app; it does not upload existing local SQLite history automatically.

Local development is a single-resident, loopback-only application. Optional
`GUARDMATE_ACCESS_USERNAME` and `GUARDMATE_ACCESS_PASSWORD` enable dashboard
authentication. Public Render hosting requires both authentication and Atlas;
see the [deployment guide](render-deployment.md).

## Start the app

Start one backend worker:

```powershell
.venv\Scripts\python -m uvicorn guardmate.main:app --app-dir backend --host 127.0.0.1 --port 8765
```

In another terminal:

```powershell
npm run dev
```

Open [GuardMate](http://127.0.0.1:5173). Local [API documentation](http://127.0.0.1:8765/docs)
is available from the backend. Restart the backend after changing `.env`.

For authenticated local use, run `npm run build` and open
[the backend origin](http://127.0.0.1:8765) so the dashboard and API share an
origin. The Vite development server serves the frontend on a separate origin.

On Ubuntu or WSL, create a separate environment with `python3 -m venv .venv` and
use `.venv/bin/python`. The tested cellular route requires Windows, not WSL.

## Optional speech and calls

Speech needs the voice dependencies and an explicit asset download:

```powershell
.venv\Scripts\python -m pip install -r backend\requirements-voice.txt
.venv\Scripts\python backend\scripts\setup_speech.py --download
```

Restart the backend, then follow the [browser voice guide](browser-voice.md).
Phone Link setup is covered in the [manual call guide](manual-cellular-call.md)
and [automatic turn-taking guide](automatic-cellular-call.md).

The local cellular CLI expects an unauthenticated loopback backend; it does not
send dashboard login credentials.

Call tests require both participants' consent. Automatic mode also requires
consent to sending unreviewed recognized text to Tinker. Use supervised fictional
calls only; the operator must answer and hang up.

## Verify changes

Run backend tests in a process that skips `.env` and clears inherited deployment
settings. This keeps tests away from the live Atlas database and leaves your
actual `.env` unchanged:

```powershell
@'
import os
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
for key in (
    "MONGODB_URI",
    "MONGODB_DATABASE",
    "GUARDMATE_ACCESS_USERNAME",
    "GUARDMATE_ACCESS_PASSWORD",
    "TINKER_API_KEY",
    "GUARDMATE_SAMPLER_CHECKPOINT",
    "RENDER",
):
    os.environ.pop(key, None)
import pytest
raise SystemExit(pytest.main())
'@ | .venv\Scripts\python -
```

Then run the static checks and frontend tests:

```powershell
.venv\Scripts\python -m ruff check backend
.venv\Scripts\python -m ruff format --check backend
npm run build
npm test
npm run format:check
```

Tests use fake providers and audio devices and need no sponsor credit. For model
experiments, see [evaluation](evaluation.md), [training](training.md),
[model selection](model-selection.md) and [speech replay](speech-replay.md).
