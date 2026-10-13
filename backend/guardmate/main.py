import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from .agent.engine import ConversationEngine
from .agent.provider import PlanProvider, TinkerProvider
from .agent.routes import build_router
from .agent.store import ConversationStore
from .context import build_context
from .models import Dashboard, DeliveryMode, OverrideRequest, ResidentProfile, TodayOverride
from .mongo import MongoPersistence
from .security import AccessMiddleware, AccessSettings, FrontendFiles
from .speech import SpeechService
from .speech.routes import build_speech_router
from .store import PreferenceStore


def create_app(
    data_dir: Path | None = None,
    clock: Callable[[], datetime] | None = None,
    provider: PlanProvider | None = None,
    speech_service: SpeechService | None = None,
    frontend_dir: Path | None = None,
) -> FastAPI:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)
    access = AccessSettings.from_environment()
    if access.render and not os.environ.get("MONGODB_URI", "").strip():
        raise RuntimeError("MONGODB_URI is required for durable GuardMate storage on Render.")
    mongo = MongoPersistence.from_environment()
    database = mongo.database if mongo is not None else None
    storage_dir = data_dir or Path(os.environ.get("GUARDMATE_DATA_DIR", str(root / ".data")))
    current_time = clock or (lambda: datetime.now(UTC))

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            if mongo is not None:
                mongo.ping()
            conversation_store.initialize()
            application.state.store = PreferenceStore(storage_dir, mongo_database=database)
            yield
        except PyMongoError:
            raise RuntimeError(
                "GuardMate storage could not be reached. Check the MongoDB configuration."
            ) from None
        finally:
            if mongo is not None:
                mongo.close()

    application = FastAPI(title="GuardMate", version="0.1.0", lifespan=lifespan)
    application.add_middleware(AccessMiddleware, settings=access)

    @application.exception_handler(PyMongoError)
    async def storage_unavailable(_request, _error):
        # Driver errors can contain connection details; never expose them in API responses.
        return JSONResponse(
            {"detail": "GuardMate storage is temporarily unavailable."}, status_code=503
        )

    def dashboard() -> Dashboard:
        store: PreferenceStore = application.state.store
        profile = ResidentProfile.model_validate(store.read("profile") or {})
        delivery_mode = DeliveryMode.model_validate(store.read("delivery_mode") or {})
        raw_override = store.read("today_override")
        override = TodayOverride.model_validate(raw_override) if raw_override else None
        now = current_time()
        return Dashboard(
            profile=profile,
            delivery_mode=delivery_mode,
            context=build_context(profile, delivery_mode, override, now),
            server_time=now,
        )

    @application.get("/api/health")
    def health() -> dict[str, str]:
        if mongo is not None:
            mongo.ping()
        return {"status": "ok", "service": "guardmate"}

    @application.get("/api/dashboard", response_model=Dashboard)
    def read_dashboard() -> Dashboard:
        return dashboard()

    @application.put("/api/profile", response_model=Dashboard)
    def save_profile(profile: ResidentProfile) -> Dashboard:
        application.state.store.write("profile", profile)
        return dashboard()

    @application.put("/api/availability", response_model=Dashboard)
    def save_availability(request: OverrideRequest) -> Dashboard:
        profile = dashboard().profile
        override = (
            TodayOverride(
                status=request.status,
                local_date=current_time().astimezone(ZoneInfo(profile.timezone)).date(),
            )
            if request.status is not None
            else None
        )
        application.state.store.write("today_override", override)
        return dashboard()

    @application.put("/api/delivery-mode", response_model=Dashboard)
    def save_delivery_mode(mode: DeliveryMode) -> Dashboard:
        if mode.enabled:
            if not dashboard().context.setup_complete:
                raise HTTPException(
                    status_code=422,
                    detail="Save your name, PG name and guard-room location first.",
                )
            if mode.expires_at is None or mode.expires_at <= current_time():
                raise HTTPException(status_code=422, detail="Choose an end time in the future.")
        application.state.store.write("delivery_mode", mode)
        return dashboard()

    conversation_store = ConversationStore(storage_dir, mongo_database=database)
    selected_provider = (
        provider
        if provider is not None
        else TinkerProvider(
            conversation_store,
            sampler_checkpoint=os.environ.get("GUARDMATE_SAMPLER_CHECKPOINT") or None,
        )
    )
    engine = ConversationEngine(
        conversation_store,
        selected_provider,
        dashboard,
        current_time,
    )
    application.include_router(build_router(engine))
    application.include_router(build_speech_router(engine, speech_service or SpeechService(root)))
    frontend = frontend_dir if frontend_dir is not None else root / "dist"
    if (frontend / "index.html").is_file():
        application.mount("/", FrontendFiles(directory=frontend), name="frontend")
    return application


app = create_app()
