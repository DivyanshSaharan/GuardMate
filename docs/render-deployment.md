# Render and MongoDB Atlas deployment

The Docker image serves the React dashboard and FastAPI API on one origin. The
hosted application stores preferences, conversations and the inference budget
ledger in MongoDB Atlas. Render startup requires Atlas and dashboard credentials;
it never falls back to an ephemeral SQLite database.

## Configuration

Create a dedicated free Atlas cluster and a database user with `readWrite` only
on the `guardmate` database. Allow the outbound IP ranges shown in the Render
service's Connect menu in the Atlas network access list. Keep TLS certificate
verification enabled and avoid allowing all internet addresses.

Create a Render Docker web service from the repository root, using `Dockerfile`,
the Free plan, and health check path `/api/health`. `render.yaml` records the
same settings for reproducible setup. Set these server-side environment variables:

| Variable                    | Value                                                          |
| --------------------------- | -------------------------------------------------------------- |
| `MONGODB_URI`               | Atlas driver URI containing the dedicated database credentials |
| `MONGODB_DATABASE`          | `guardmate`                                                    |
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
Speech quality, memory consumption and latency must be tested on the deployed
instance; Render Free has 512 MB RAM and limited CPU, and idles after inactivity.
The container uses one speech thread and disables ONNX thread spinning to fit
small CPU allocations. `GUARDMATE_SPEECH_TIMEOUT` can set a bounded 5–120 second
timeout; the default is 45 seconds. A speech timeout does not authorize a handoff.
Windows Phone Link, Bluetooth audio and cellular calling remain local Windows
features. Public hosting does not make those hardware integrations remote.

Existing local SQLite data is not automatically uploaded. Local test and training
tools retain their original SQLite storage when Atlas is not configured. The
separate training ledger is not part of the deployed web application.

References: [Render Docker](https://render.com/docs/docker),
[Render Free limits](https://render.com/docs/free),
[Atlas connectivity](https://www.mongodb.com/docs/atlas/connect-to-database-deployment/).
