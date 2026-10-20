# Windows desktop and cloud gateway

GuardMate's public website and account-linked Windows downloads are live at
https://guardmate.onrender.com. Render's release metadata selects the current
runtime and installer template stored privately in R2. Google sign-in creates a
personalized installer; the common bootstrap template cannot be installed alone.

## Responsibilities

The Windows app starts a loopback FastAPI server and opens its bundled dashboard.
Each account's preferences/history live in local SQLite under
%LOCALAPPDATA%\GuardMate\accounts\<account-id>\data. Models are shared locally
under %LOCALAPPDATA%\GuardMate\models\speech. Sessions use CurrentUser DPAPI.

Whisper/Piper run locally in bounded workers. Reviewed text, history and resident
instructions go through Render to Qwen/Tinker. Atlas holds verified account
identities, hashed credentials, expiring sessions and estimated reservations.
New cloud records do not store prompts, plans, resident profiles or call audio.
The public website's prepared example needs no Basic-auth login.

## Google OAuth

Create a **Web application** client. Authorized JavaScript origins:

    https://guardmate.onrender.com
    http://localhost:8765
    http://localhost:5173

Authorized redirect URIs:

    https://guardmate.onrender.com/api/cloud/auth/google/callback
    http://localhost:8765/api/cloud/auth/google/callback

Use localhost consistently. The callback is on backend port 8765; 5173 is the
Vite development frontend. The installed app uses the Render account credential,
so its random local port needs no Google callback registration.

Set GUARDMATE_GOOGLE_CLIENT_ID and GUARDMATE_GOOGLE_CLIENT_SECRET server-side.
GUARDMATE_PUBLIC_ORIGIN is the Render HTTPS origin in production and
http://localhost:8765 for local cloud testing. Add intended users to the consent
screen's test-user list while Google publishing status is Testing.

## R2

Open **Storage & databases > R2 > Overview**. Enable R2 if needed, completing
any billing/terms prompts yourself. Create a private **Standard** bucket named
guardmate-releases with default jurisdiction. Keep public access disabled.

Create two S3 tokens scoped only to this bucket: **Object Read & Write** locally
for publishing, and **Object Read only** on Render.

    GUARDMATE_R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
    GUARDMATE_R2_BUCKET=guardmate-releases
    GUARDMATE_R2_ACCESS_KEY_ID=<Render read-only key>
    GUARDMATE_R2_SECRET_ACCESS_KEY=<Render read-only secret>
    GUARDMATE_UPLOAD_R2_ACCESS_KEY_ID=<local upload key>
    GUARDMATE_UPLOAD_R2_SECRET_ACCESS_KEY=<local upload secret>

Keep credentials in private environment settings. Never include them in browser
assets, runtime ZIPs, installers, screenshots or chat.

## Build and publish

Use Windows x64, Python 3.13, the development dependencies,
backend/requirements-desktop.txt, and PyInstaller 6.22.3.

    .venv\Scripts\python -m pip install -r backend\requirements-desktop.txt pyinstaller==6.22.3
    .venv\Scripts\python backend\scripts\setup_speech.py --download
    npm ci
    npm run build
    powershell -NoProfile -File packaging\windows\compile_bootstrap.ps1
    powershell -NoProfile -File packaging\windows\build.ps1 -Version 0.2.0

Outputs are ignored under .cache/windows-release. Use a new immutable version
for each release. The common runtime contains Python, the dashboard, native
libraries, licences and pinned asset manifests. Setup downloads verified speech
models separately, reuses valid models and repairs corrupt downloads.

Prepare release metadata without uploading:

    .venv\Scripts\python packaging\windows\publish.py --version 0.2.0 --runtime .cache\windows-release\GuardMate-runtime-0.2.0-win-x64.zip --installer .cache\windows-release\GuardMateSetup.template.exe

With explicit upload environment keys configured, add --upload. The publisher
uploads common files privately and reads them back to verify hashes/lengths.
Existing matching files are reusable; release keys are never overwritten.
It writes .cache/windows-release/render-release.json. Put its two JSON values
into GUARDMATE_DESKTOP_RELEASE and GUARDMATE_INSTALLER_RELEASE on Render.

The initial bootstrap is unsigned. After login, Render appends a bounded,
versioned account-credential envelope to the common EXE. The bootstrap verifies
that frame, activates only against https://guardmate.onrender.com, and verifies
the downloaded runtime's SHA-256 before extracting it. It never contains the
owner's Tinker/MongoDB keys. Copied installers intentionally share an account.
Only the small template and common runtime are stored in R2; personalized EXEs
are generated on demand and must not be uploaded to public releases.

## Deploy and test locally

Docker runs the cloud factory with one worker, without native speech models.
It requires MONGODB_URI, MONGODB_DATABASE=guardmate and TINKER_API_KEY, plus the
Google/R2/release settings above. It never mounts the old resident API or local
desktop API. Old Basic-auth variables are ignored by this factory.

For local cloud testing, install backend/requirements-cloud.txt and configure
Google plus a separate testing Atlas database before running:

    $env:GUARDMATE_MODE = 'cloud'
    $env:GUARDMATE_PUBLIC_ORIGIN = 'http://localhost:8765'
    .venv\Scripts\python -m uvicorn guardmate.main:app --app-dir backend --host 127.0.0.1 --port 8765 --no-access-log

Open http://localhost:8765 after building the frontend. Missing OAuth/R2 settings
leave the website public while protected operations fail closed. Automated tests
disable .env and inherited live credentials in backend/tests/conftest.py.

## Budgets and historical data

Each verified account starts with a $1 lifetime estimated allowance. Identity
comes from Google's verified subject. Download, reinstall, credential renewal
and activation never allocate another allowance.

The provider tokenizes using its actual tokenizer and reserves a conservative
input/output bound in integer microdollars. A MongoDB transaction atomically
creates a unique account/request record and reserves funds before dispatch.
External model calls never run in database transaction callbacks. Changed
payloads under the same ID and duplicate submissions are rejected.

Completed and uncertain attempts retain conservative reservations. Unknown
outcomes never automatically refund or resend. This is an estimated application
allowance, not a provider invoice balance. Sampling concurrency is bounded on
the small server. Website account controls can revoke individual downloads.

Historical model_budget records remain untouched. After the owner signs in,
preview their one-time migration using explicit Atlas environment settings and
PYTHONPATH=backend:

    .venv\Scripts\python -m guardmate.cloud.migrate_legacy --account-id <owner-account-id>

Review the dry-run, then add --apply. The transaction imports the authoritative
old reservation once and rejects different owners or changed source balances.
Never clear account/request collections to repair a release.

## Desktop use

1. Sign in on the website, download your personalized EXE, and run setup.
2. Open Phone Link through GuardMate; complete Microsoft sign-in, pairing,
   Bluetooth Calls and phone permissions yourself.
3. Save resident instructions and enable a delivery window.
4. Scan audio metadata and explicitly select both paired-phone WDM-KS endpoints.
5. Manually answer a consenting call and start a fresh assisted-call session.
6. Capture a short caller turn, review/edit the transcript and send it.
7. Choose Play separately to transmit the saved reply. Use resident controls
   for approve-once, decline or takeover, and refresh the active call afterward.
8. Stop assistance, hang up through Phone Link, then Quit GuardMate when finished.

Physical answering/hangup remain manual. Metadata cannot verify an active call
or caller identity. No laptop microphone/speaker fallback is allowed. Updating
is refused while the desktop runtime's mutex exists. Uninstall removes the app
and session while preserving local models/preferences and the server allowance.

Automatic turn-taking stays experimental. Validate a supervised fictional call
on each new phone/computer; the older CLI report does not prove this packaged GUI
works across every Phone Link-supported device.

## Rollback

Keep prior versioned R2 objects. Roll back the compatible server commit and
release JSON together if necessary. Preserve all accounting and dedup records.
