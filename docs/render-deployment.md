# Render and MongoDB Atlas deployment

The Docker image serves the React dashboard and FastAPI API on one origin. The
hosted application stores preferences, conversations and the inference budget
ledger in MongoDB Atlas. Render startup requires Atlas and dashboard credentials;
it never falls back to an ephemeral SQLite database.

## Configuration

Create a dedicated free Atlas cluster and a database user with `readWrite` only
on the `guardmate` database. The recommended Atlas network access list allows
the outbound IP ranges shown in the Render service's Connect menu. Keep TLS
certificate verification enabled.

For this deployment, the owner explicitly requested an Atlas `0.0.0.0/0`
network access entry. It allows connections from any IPv4 address; the dedicated
database credentials and TLS verification remain required. This is a deployment
exception to the recommended restricted Render outbound ranges above.

Create a Render Docker web service from the repository root, using `Dockerfile`,
the owner-approved Starter target ($7/month, 0.5 CPU and 512 MB RAM), and health
check path `/api/health`. `render.yaml` records this target for reproducible setup.
The upgrade is pending: Render requires payment information on file, so the
current service remains on Free (0.1 CPU and 512 MB RAM) until the upgrade succeeds.
Render requires this payment setup even when promotional credits are available.

Set these server-side environment variables:

| Variable                    | Value                                                          |
| --------------------------- | -------------------------------------------------------------- |
| `MONGODB_URI`               | Atlas driver URI containing the dedicated database credentials |
| `MONGODB_DATABASE`          | `guardmate`                                                    |
| `GUARDMATE_SPEECH_TIMEOUT`  | `90` seconds                                                   |
| `TINKER_API_KEY`            | Existing hosted inference API key                              |
| `GUARDMATE_ACCESS_USERNAME` | Dedicated dashboard username                                   |
| `GUARDMATE_ACCESS_PASSWORD` | Strong randomly generated dashboard password                   |

Optional `GUARDMATE_SAMPLER_CHECKPOINT` selects an evaluated sampler export.
The base Qwen model is used when it is absent. Never put database or model secrets
in a `VITE_` variable, Git, the Dockerfile, or a frontend asset. No secret files
are needed: Render environment variables supply all application credentials.

The image runs a single Uvicorn worker as an unprivileged user, binds Render's
`PORT`, and trusts proxy headers only within the Render deployment. HTTP Basic
authentication protects the dashboard, API and documentation over Render HTTPS.
The minimal health endpoint is public for service monitoring. Unsafe browser
requests from other origins are rejected. Keep dashboard credentials private:
this is still a single-resident application, not a multi-user service.

## Verification and limits

After deployment, check `/api/health`, verify anonymous access to the dashboard
and `/api/dashboard` returns an authentication challenge, then sign in and save
a fictional preference. Restart the service and confirm that the preference and
budget ledger still exist in Atlas.

The Linux image includes English browser speech assets built from the pinned,
verified upstream sources. Whisper uses a derived Q5_0 quantized `base.en` model.
The live Free instance passed one synthetic English greeting round-trip:
Piper synthesis returned HTTP 200 in 52.1 seconds and Whisper transcription
returned HTTP 200 in 59.8 seconds. Free hosting is functional but slow; speech
latency on Starter must be checked after the upgrade. This check does not cover
human microphone recordings, telephony or concurrent traffic.
The container uses one speech thread and disables ONNX thread spinning to fit
small CPU allocations. `GUARDMATE_SPEECH_TIMEOUT` can set a bounded 5–120 second
timeout; the local default is 45 seconds. The deployment configuration sets it
to 90 seconds for native speech on the limited CPU allocation. A speech timeout
does not authorize a handoff.
Windows Phone Link, Bluetooth audio and cellular calling remain local Windows
features. Public hosting does not make those hardware integrations remote.

Existing local SQLite data is not automatically uploaded. Local test and training
tools retain their original SQLite storage when Atlas is not configured. The
separate training ledger is not part of the deployed web application.

References: [Render Docker](https://render.com/docs/docker),
[Render compute plans](https://render.com/docs/compute-plans),
[Render pricing](https://render.com/pricing),
[Atlas connectivity](https://www.mongodb.com/docs/atlas/connect-to-database-deployment/).
