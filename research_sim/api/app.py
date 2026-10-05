from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

from fastapi import FastAPI

from .routes import router
from .schemas import HealthResponse
from ..database import Database, DatabaseRequests
from ..integrations.telegram import TelethonSender
from ..services.campaign import CampaignService
from ..services.accounts import SenderAccountService
from ..services.auto_reply import AutoReplyRuntime
from ..services.messaging import MessagingService
from ..services.prompt_responder import PromptResponder
from ..services.persona_research import PersonaResearchService
from ..services.personas import PersonaService
from ..services.activation_sync import ActivationSyncService
from ..settings import Settings


def create_app(
    settings: Optional[Settings] = None,
    database: Optional[Database] = None,
    client_factory: Optional[Callable[..., Any]] = None,
) -> FastAPI:
    app_settings = settings or Settings.from_environment()
    app_database = database or Database(app_settings.database_path)
    database_requests = DatabaseRequests(app_database)
    telegram_sender = TelethonSender(app_settings, client_factory=client_factory)
    persona_service = PersonaService(
        database_requests,
        corpus_path=app_settings.persona_corpus_path,
    )
    prompt_responder = PromptResponder(app_settings)
    messaging_service = MessagingService(
        app_settings,
        database_requests,
        telegram_sender,
        personas=persona_service,
        prompt_responder=prompt_responder,
    )
    campaign_service = CampaignService(
        app_settings,
        database_requests,
        messaging_service,
    )
    account_service = SenderAccountService(database_requests)
    persona_research_service = PersonaResearchService(
        database_requests,
        persona_service,
        prompt_responder,
    )
    auto_reply_runtime = AutoReplyRuntime(
        app_settings,
        database_requests,
        telegram_sender,
        messaging_service,
        prompt_responder,
    )
    activation_sync_service = ActivationSyncService(
        app_settings,
        database_requests,
        messaging_service,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        app_database.initialize()
        await auto_reply_runtime.start()
        await activation_sync_service.start()
        try:
            yield
        finally:
            await activation_sync_service.stop()
            await auto_reply_runtime.stop()

    app = FastAPI(
        title="Telegram Research API",
        version="2.0.0",
        description="API for consent-based, allowlisted Telegram test messages.",
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.database = app_database
    app.state.database_requests = database_requests
    app.state.messaging_service = messaging_service
    app.state.campaign_service = campaign_service
    app.state.account_service = account_service
    app.state.persona_research_service = persona_research_service
    app.state.auto_reply_runtime = auto_reply_runtime
    app.state.activation_sync_service = activation_sync_service

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    app.include_router(router)
    return app


app = create_app()
