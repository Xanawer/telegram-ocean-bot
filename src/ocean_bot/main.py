from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ocean_bot.application.analytics import AnalyticsService
from ocean_bot.application.assessments import AssessmentService
from ocean_bot.application.privacy import PrivacyService
from ocean_bot.config import Settings
from ocean_bot.domain.catalog import load_bundled_questionnaires
from ocean_bot.infrastructure.database import (
    SqlAnalyticsRepository,
    SqlAssessmentRepository,
    SqlPrivacyRepository,
    session_factory,
)
from ocean_bot.telegram.handlers import router
from ocean_bot.telegram.middleware import RateLimitMiddleware

_WEBHOOK_PATH = "/telegram/webhook"
_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RetentionState:
    healthy: bool = True


def _runtime(settings: Settings) -> tuple[Bot, Dispatcher, AsyncEngine]:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sessions = session_factory(engine)
    questionnaires = load_bundled_questionnaires()
    assessment_service = AssessmentService(SqlAssessmentRepository(sessions), questionnaires)
    analytics = AnalyticsService(
        SqlAnalyticsRepository(sessions),
        settings.analytics_pepper.get_secret_value(),
    )
    privacy = PrivacyService(SqlPrivacyRepository(sessions), analytics)
    dispatcher = Dispatcher(
        service=assessment_service,
        analytics=analytics,
        privacy=privacy,
        questionnaires=questionnaires,
        privacy_contact=settings.privacy_contact,
        admin_user_id=settings.admin_user_id,
    )
    rate_limit = RateLimitMiddleware()
    router.message.middleware(rate_limit)
    router.callback_query.middleware(rate_limit)
    dispatcher.include_router(router)
    bot = Bot(settings.bot_token.get_secret_value())
    return bot, dispatcher, engine


async def _set_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="test", description="Start or resume a Big Five test"),
            BotCommand(command="results", description="Show your latest result"),
            BotCommand(command="export", description="Download your stored data"),
            BotCommand(command="delete_me", description="Delete your stored data"),
            BotCommand(command="privacy", description="Read the privacy notice"),
            BotCommand(command="help", description="Show available commands"),
        ]
    )


async def _run_retention(
    service: AssessmentService,
    analytics: AnalyticsService,
    state: RetentionState,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    while True:
        try:
            now = datetime.now(UTC)
            await service.expire_incomplete(now)
            await analytics.purge(now)
        except asyncio.CancelledError:
            raise
        except Exception:
            state.healthy = False
            _logger.exception("Retention cycle failed; retrying")
            await sleep(60)
            continue
        state.healthy = True
        await sleep(24 * 60 * 60)


async def _polling(settings: Settings) -> None:
    bot, dispatcher, engine = _runtime(settings)
    service = dispatcher.workflow_data["service"]
    analytics = dispatcher.workflow_data["analytics"]
    if not isinstance(service, AssessmentService) or not isinstance(analytics, AnalyticsService):
        raise RuntimeError("Dispatcher services are misconfigured")
    await bot.delete_webhook(drop_pending_updates=False)
    await _set_commands(bot)
    retention = asyncio.create_task(_run_retention(service, analytics, RetentionState()))
    try:
        await dispatcher.start_polling(bot)
    finally:
        retention.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await retention
        await engine.dispose()


def _webhook_app(settings: Settings) -> web.Application:
    bot, dispatcher, engine = _runtime(settings)
    service = dispatcher.workflow_data["service"]
    analytics = dispatcher.workflow_data["analytics"]
    if not isinstance(service, AssessmentService) or not isinstance(analytics, AnalyticsService):
        raise RuntimeError("Dispatcher services are misconfigured")

    retention_state = RetentionState()

    async def health(_: web.Request) -> web.Response:
        if not retention_state.healthy:
            return web.json_response(
                {"status": "degraded", "retention": "retrying"},
                status=503,
            )
        return web.json_response({"status": "ok"})

    async def startup(_: web.Application) -> None:
        await _set_commands(bot)
        base_url = (settings.webhook_base_url or "").rstrip("/")
        secret = settings.webhook_secret
        await bot.set_webhook(
            base_url + _WEBHOOK_PATH,
            secret_token=secret.get_secret_value() if secret else None,
        )

    async def retention_context(_: web.Application) -> AsyncIterator[None]:
        task = asyncio.create_task(_run_retention(service, analytics, retention_state))
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def cleanup(_: web.Application) -> None:
        await engine.dispose()
        await bot.session.close()

    app = web.Application()
    app.router.add_get("/health", health)
    app.on_startup.append(startup)
    app.cleanup_ctx.append(retention_context)
    app.on_cleanup.append(cleanup)
    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=settings.webhook_secret.get_secret_value()
        if settings.webhook_secret
        else None,
    ).register(app, path=_WEBHOOK_PATH)
    setup_application(app, dispatcher, bot=bot)
    return app


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings()  # type: ignore[call-arg]
    if settings.webhook_enabled:
        web.run_app(_webhook_app(settings), port=settings.port)
    else:
        asyncio.run(_polling(settings))
