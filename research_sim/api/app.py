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
    messaging_service = MessagingService(
        app_settings,
        database_requests,
        telegram_sender,
    )
    campaign_service = CampaignService(
        app_settings,
        database_requests,
        messaging_service,
    )
    account_service = SenderAccountService(database_requests)
    auto_reply_runtime = AutoReplyRuntime(
        app_settings,
        database_requests,
        telegram_sender,
        messaging_service,
        PromptResponder(app_settings),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        app_database.initialize()
        await auto_reply_runtime.start()
        try:
            yield
        finally:
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
    app.state.auto_reply_runtime = auto_reply_runtime

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    app.include_router(router)
    return app


app = create_app()