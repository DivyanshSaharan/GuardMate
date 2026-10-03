# GuardMate

A personal AI delivery assistant built for a friend who lives in a PG. The intended voice agent talks to couriers, follows approved guard-room instructions and asks for help when a handoff needs the resident's decision.

## Current functionality

This first increment saves the resident's PG instructions, weekly office routine, daily availability override and expiring delivery-mode window. Preferences are stored in a local SQLite database and survive application restarts. Schedule calculations use Asia/Kolkata; daily overrides expire at local midnight.

The interface shows the current courier instruction and supports copying it. Calls are not answered in this increment. The planned Qwen3.5-4B/Tinker dialogue agent and speech pipeline will consume this saved delivery context in subsequent increments.

## Run locally

Use Node.js 22.12+ (or a newer supported Node release) and Python 3.13+. The current setup was tested with Node.js 26 and Python 3.13 on Windows.

From the repository root:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r backend\requirements-dev.txt
npm install
```

Start the backend in one terminal:

```powershell
.venv\Scripts\python -m uvicorn guardmate.main:app --app-dir backend --host 127.0.0.1 --port 8765
```

Start the frontend in a second terminal:

```powershell
npm run dev
```

Open <http://127.0.0.1:5173>. API documentation is available at <http://127.0.0.1:8765/docs>.

On Ubuntu, create the virtual environment with `python3 -m venv .venv` and use `.venv/bin/python` in place of `.venv\Scripts\python`. A venv should be created independently for each operating system rather than shared between Windows and WSL.

## Verify

```powershell
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check backend
.venv\Scripts\python -m ruff format --check backend
npm run build
npm test
npm run format:check
```

Tests cover persisted preferences, office-time boundaries, configurable office days, local-midnight override expiry, delivery-window expiry and invalid settings. No model API or sponsor credit is needed for this increment.

Frontend regression tests cover section render isolation, scoped pending controls, draft preservation, notification dismissal, unchanged polling snapshots and stale-response protection. The dashboard is composed of separate availability, delivery-mode, instruction-preview and preferences components. Form drafts, delivery-window edits and clipboard feedback stay local to their sections. Notifications render in a fixed-position portal, so appearing or disappearing messages do not move the page content.

## Data and configuration

Runtime data is saved to `.data/guardmate.sqlite3`, which is excluded from Git. Set `GUARDMATE_DATA_DIR` to choose another storage directory. Dependencies, runtime caches, secrets, model weights and checkpoints are also excluded from Git.

The dashboard is intended for the local resident and both development services bind to loopback by default. Authentication must be added before exposing resident settings on a public server.

## Planned AI stack

- Qwen3.5-4B with supervised LoRA training through Tinker.
- whisper.cpp for speech recognition and Piper for speech generation.
- Validated tools for handoff instructions, approval requests and caller-reported outcomes.
- A browser voice interface for independent agent testing.
- Asterisk/BlueZ/AudioSocket for a cellular prototype after two-way Bluetooth audio is verified.

Ubuntu in WSL supports development, but phone-call access additionally requires a compatible Bluetooth device exposed to Linux. The presence of a Linux distribution alone does not establish that connection.
