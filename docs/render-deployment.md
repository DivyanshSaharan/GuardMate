# Render, Atlas and private release hosting

The cloud image serves the public React website and an account-scoped FastAPI
gateway. Windows runs Phone Link, Whisper, Piper, policy checks and local SQLite.
The Render image does not contain speech models or mount resident-data APIs.

The public gateway and account-linked downloads are published. The current
Windows release is `0.2.0-rc.2`; its bootstrap handles Windows ZIP separators
and .NET Framework speech-manifest arrays. Physical Phone Link calls still
require supervised verification on the installed release.

## Responsibilities and access

The website and prepared example are public. Google sign-in creates an expiring
browser session for viewing the account allowance, downloading a personalized
installer and revoking downloads. Desktop activation creates a separate API
session. Both resolve to a permanent account identity derived from Google's
verified subject; downloads and reinstalls never allocate another allowance.

Atlas stores minimal account identity, hashed credentials, expiring sessions and
request reservations. Preferences, conversation history and raw call audio stay
on the Windows computer. Reviewed text, history and resident instructions travel
through the authenticated gateway to Tinker. New cloud records do not persist
those prompts or returned plans.

The cloud factory ignores the old Basic-auth settings and never mounts the
previous hosted dashboard, conversation or speech APIs. Existing private Atlas
records are retained and are not exposed through the public website.

## Render configuration

Use the repository-root `Dockerfile`, the owner-approved Starter service and
health path `/api/health`. The image runs one Uvicorn worker as an unprivileged
user, binds Render's `PORT` and disables request access logs. `render.yaml`
describes the cloud service; automatic deployment is disabled so releases can
be verified before publication.

Set the following in Render's private environment settings:

| Variable                         | Value                                           |
| -------------------------------- | ----------------------------------------------- |
| `GUARDMATE_MODE`                 | `cloud`                                         |
| `GUARDMATE_PUBLIC_ORIGIN`        | `https://guardmate.onrender.com`                |
| `MONGODB_URI`                    | TLS Atlas URI for the dedicated database user   |
| `MONGODB_DATABASE`               | `guardmate`                                     |
| `TINKER_API_KEY`                 | Owner's hosted inference credential             |
| `GUARDMATE_GOOGLE_CLIENT_ID`     | Google Web application client ID                |
| `GUARDMATE_GOOGLE_CLIENT_SECRET` | Matching Google client secret                   |
| `GUARDMATE_R2_ENDPOINT`          | `https://<account-id>.r2.cloudflarestorage.com` |
| `GUARDMATE_R2_BUCKET`            | Private `guardmate-releases` bucket             |
| `GUARDMATE_R2_ACCESS_KEY_ID`     | Bucket-scoped Object Read only key              |
| `GUARDMATE_R2_SECRET_ACCESS_KEY` | Matching read-only secret                       |
| `GUARDMATE_DESKTOP_RELEASE`      | Publisher-generated runtime metadata JSON       |
| `GUARDMATE_INSTALLER_RELEASE`    | Publisher-generated installer metadata JSON     |

The cloud gateway uses base Qwen3.5-4B. Local training checkpoints and the legacy
speech timeout settings do not select the cloud runtime. Keep all provider,
database and storage secrets out of Git, `VITE_` variables and release packages.
No secret files need to be deployed; environment variables supply the credentials.

## Google sign-in

Create a Google OAuth **Web application** client. Register the production
JavaScript origin `https://guardmate.onrender.com` and exact redirect URI:

```text
https://guardmate.onrender.com/api/cloud/auth/google/callback
```

Authorization uses a server-side code exchange, PKCE, a browser-bound state
cookie and a nonce. Google tokens are verified before any account is created.
Production session cookies are Secure, HttpOnly and SameSite=Lax.

While the consent screen is in Testing, add intended users to Google's test-user
list. The installed app uses an account credential against Render, so its random
loopback port needs no Google OAuth redirect registration. Local cloud-testing
origins and callbacks are listed in [the desktop guide](desktop-release.md#google-oauth).

## Atlas and accounting

Use a database user with `readWrite` restricted to `guardmate`, with TLS
verification enabled. The owner explicitly selected Atlas network access
`0.0.0.0/0`; the database credentials and TLS remain required. Render startup
pings Atlas and creates the account/request indexes. There is no cloud SQLite
fallback or Render disk requirement for this ledger.

Each verified account starts with a **$1 lifetime estimated allowance**. The
provider tokenizes the actual input and reserves input cost plus the bounded
maximum output in integer microdollars. A MongoDB transaction atomically checks
the account allowance, increments its reservation and inserts the unique
account/request record. The external model call happens after that transaction.

Concurrent copies share the same account. Duplicate requests and changed payloads
under the same ID are rejected. Completed and uncertain outcomes retain their
conservative reservations; the gateway does not automatically refund, resend or
reconcile to a provider invoice. Inference concurrency is limited to one request
per process. This is application accounting, not a provider billing guarantee.

Historical `model_budget` records are never cleared by cloud startup. After the
owner has signed in, use the explicit dry-run and one-time migration documented
in [the desktop guide](desktop-release.md#budgets-and-historical-data). Preserve
all account and request records through deployments and rollbacks.

## R2 releases

Keep the Standard bucket private, without a public development URL or domain.
Render needs Object Read only credentials; the local publisher uses separate
Object Read & Write credentials scoped to the same bucket. Upload credentials
must not be added to Render or the desktop app.

Build and verify the Windows runtime and unsigned bootstrap template on Windows.
Publish immutable versioned objects, read them back and verify their sizes and
SHA-256 hashes. The publisher writes the two release metadata values for Render;
these must describe the same verified build. See
[build and publish](desktop-release.md#build-and-publish) for commands.

The gateway verifies the private template, then appends the account credential
envelope to generate a personalized `GuardMateSetup.exe`. Personalized installers
are not stored as public release assets. Copied installers intentionally activate
the same account. Setup obtains a temporary private runtime URL, verifies the
package and downloads pinned speech models locally. Installed sessions are
protected with Windows CurrentUser DPAPI.

## Deploy and verify

1. **Publish** the verified common runtime and installer template to private R2.
2. **Configure** Google, Atlas, Tinker, R2 and both generated release JSON values.
3. **Deploy** the intended commit and confirm that Render reports it healthy.
4. **Check** `/api/health` and `/api/runtime`; runtime mode must be `cloud`.
5. **Visit** `/` anonymously. It should show the website without a Basic-auth
   challenge. `/api/cloud/config` should report sign-in and downloads ready.
6. **Confirm** `/api/dashboard`, `/api/conversations`, `/api/speech/status` and
   `/api/desktop/status` return 404. Anonymous account and download access must
   remain denied.
7. **Sign** in through Google, download an account-bound installer, then verify
   installation, activation and the displayed account allowance.
8. **Validate** a bounded fictional inference request, copied-installer account
   identity and revoked-download behaviour. Keep physical call validation separate
   and supervised; metadata alone does not prove phone audio works.

Automated tests disable dotenv and inherited deployment credentials. Use a
separate database for manual development. Do not run source experiments against
the production account ledger merely to test configuration.

For rollback, keep previous R2 objects and restore a compatible server commit
and release metadata together. Preserve the durable ledger and duplicate records.

## Historical hosted prototype

The deployment documented on **October 13, 2026** used Basic authentication,
hosted resident preferences/history, a shared estimated reservation ledger and
Linux browser-speech workers. That architecture is superseded by the desktop
client and cloud gateway described above; its private records remain preserved.

On that earlier Starter instance, one synthetic English greeting round-trip
returned Piper audio in **14.3 seconds** and Whisper transcription in
**14.9 seconds**. It made no hosted model request and did not test human
microphones, cellular audio or concurrent traffic. Those single measurements do
not describe local desktop speech performance. The previous Linux image used a
Q5_0 quantization of `base.en`; the Windows release uses its pinned local assets.

The [October 4, 2026 Phone Link report](live-cellular-test-2026-10-04.md) describes
a separate manually answered CLI call on one vivo phone. It does not establish
packaged GUI compatibility or unattended calling. Phone Link setup, Bluetooth,
answering and hangup remain local Windows responsibilities.

References: [Render Docker](https://render.com/docs/docker),
[Atlas connectivity](https://www.mongodb.com/docs/atlas/connect-to-database-deployment/),
[R2 private download links](https://developers.cloudflare.com/r2/api/s3/presigned-urls/).
