"""Public website and account-scoped inference; never mount the resident data API."""

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from .cloud.auth import GoogleOAuth, GoogleSettings
from .cloud.downloads import R2Downloads, Release
from .cloud.gateway import CloudService
from .cloud.routes import build_cloud_router
from .cloud.store import MongoCloudStore
from .mongo import MongoPersistence
from .security import AccessMiddleware, AccessSettings, FrontendFiles


def create_cloud_app(frontend_dir: Path | None = None, *, persistence=None) -> FastAPI:
    mongo = persistence if persistence is not None else MongoPersistence.from_environment()
    store = MongoCloudStore(mongo.database) if mongo is not None else None
    service = CloudService(store) if store is not None else None
    origin = os.environ.get("GUARDMATE_PUBLIC_ORIGIN", "https://guardmate.onrender.com").rstrip("/")
    client_id = os.environ.get("GUARDMATE_GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GUARDMATE_GOOGLE_CLIENT_SECRET", "").strip()
    oauth = (
        GoogleOAuth(store, GoogleSettings(client_id, client_secret, origin))
        if store is not None and client_id and client_secret
        else None
    )
    downloads = None
    release_json = os.environ.get("GUARDMATE_DESKTOP_RELEASE", "")
    installer_json = os.environ.get("GUARDMATE_INSTALLER_RELEASE", "")
    if release_json and installer_json and store is not None:
        try:
            downloads = R2Downloads.from_credentials(
                endpoint=os.environ["GUARDMATE_R2_ENDPOINT"],
                access_key=os.environ["GUARDMATE_R2_ACCESS_KEY_ID"],
                secret_key=os.environ["GUARDMATE_R2_SECRET_ACCESS_KEY"],
                bucket=os.environ["GUARDMATE_R2_BUCKET"],
                release=Release(**json.loads(release_json)),
                installer=Release(**json.loads(installer_json)),
            )
        except (KeyError, TypeError, ValueError):
            # Partial/invalid deployment configuration cannot enable downloads.
            downloads = None

    @asynccontextmanager
    async def lifespan(_application):
        try:
            if mongo is not None:
                mongo.ping()
                store.initialize()
            yield
        finally:
            if mongo is not None:
                mongo.close()

    application = FastAPI(
        title="GuardMate Cloud",
        version="0.2.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Header hardening only. Cloud identity routes enforce their own authentication.
    application.add_middleware(AccessMiddleware, settings=AccessSettings())

    @application.exception_handler(PyMongoError)
    async def storage_unavailable(_request, _error):
        return JSONResponse({"detail": "cloud_unavailable"}, status_code=503)

    @application.get("/api/health")
    def health():
        if mongo is not None:
            mongo.ping()
        return {"status": "ok", "service": "guardmate"}

    @application.get("/api/runtime")
    def runtime():
        return {"mode": "cloud", "version": "0.2.0"}

    application.include_router(
        build_cloud_router(service, oauth=oauth, downloads=downloads, public_origin=origin)
    )
    frontend = frontend_dir or Path(__file__).resolve().parents[2] / "dist"
    if (frontend / "index.html").is_file():
        application.mount("/", FrontendFiles(directory=frontend), name="frontend")
    return application
