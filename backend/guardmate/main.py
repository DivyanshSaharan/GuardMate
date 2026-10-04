import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

from .agent.engine import ConversationEngine
from .agent.provider import PlanProvider, TinkerProvider
from .agent.routes import build_router
from .agent.store import ConversationStore
from .context import build_context
from .models import Dashboard, DeliveryMode, OverrideRequest, ResidentProfile, TodayOverride
from .speech import SpeechService
from .speech.routes import build_speech_router
from .store import PreferenceStore


def create_app(
    data_dir: Path | None = None,
    clock: Callable[[], datetime] | None = None,
    provider: PlanProvider | None = None,
    speech_service: SpeechService | None = None,
) -> FastAPI:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)
    storage_dir = data_dir or Path(os.environ.get("GUARDMATE_DATA_DIR", str(root / ".data")))
    current_time = clock or (lambda: datetime.now(UTC))

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.store = PreferenceStore(storage_dir)
        yield

    application = FastAPI(title="GuardMate", version="0.1.0", lifespan=lifespan)

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

    conversation_store = ConversationStore(storage_dir)
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
    return application


app = create_app()
