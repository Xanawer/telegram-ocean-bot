from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ocean_bot.application.analytics import AnalyticsRepository, AnalyticsService
from ocean_bot.application.assessments import AssessmentRepository, AssessmentService
from ocean_bot.application.data_rights import (
    UserDataRights,
    UserDataRightsRepository,
    UserReferences,
)
from ocean_bot.application.interactions import AssessmentInteraction
from ocean_bot.application.retention import DataRetention, RetentionHealth, RetentionResult
from ocean_bot.config import Settings
from ocean_bot.domain.catalog import load_bundled_questionnaires
from ocean_bot.domain.questionnaire import Questionnaire
from ocean_bot.infrastructure.database import (
    SqlAnalyticsRepository,
    SqlAssessmentRepository,
    SqlUserDataRightsRepository,
    session_factory,
)
from ocean_bot.infrastructure.local import LocalStorageGuard, create_local_engine
from ocean_bot.infrastructure.migrations import upgrade_database
from ocean_bot.infrastructure.replication import (
    AuthoritativeAnalyticsRepository,
    LocalAssessmentEventWriter,
    LocalBootstrapService,
    LocalFirstAssessmentRepository,
    LocalFirstUserDataRightsRepository,
    MirroringAnalyticsRepository,
    ReplicationMode,
    RetryingAnalyticsRepository,
    RetryingAssessmentRepository,
    RetryingUserDataRightsRepository,
    SyncDrainResult,
    SyncHealth,
    SyncOutboxRepository,
    SyncService,
)
from ocean_bot.telegram.handlers import create_router
from ocean_bot.telegram.middleware import RateLimitMiddleware, UserActionLocks

_WEBHOOK_PATH = "/telegram/webhook"
_logger = logging.getLogger(__name__)


def _create_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(
        database_url,
        pool_pre_ping=False,
        pool_recycle=300,
    )


@dataclass(frozen=True, slots=True)
class DatabaseModules:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    local_engine: AsyncEngine | None
    questionnaires: Mapping[str, Questionnaire]
    user_references: UserReferences
    assessments: AssessmentService
    analytics: AnalyticsService
    retention: DataRetention
    sync: SyncService | None
    sync_health: SyncHealth | None
    rights_repository: UserDataRightsRepository
    bootstrap: LocalBootstrapService | None

    async def prepare(self, local_database_url: str | None) -> None:
        if self.local_engine is not None and local_database_url is not None:
            await upgrade_database(local_database_url)
            if self.bootstrap is not None:
                await self.bootstrap.run()

    async def close(self) -> None:
        await self.engine.dispose()
        if self.local_engine is not None:
            await self.local_engine.dispose()


@dataclass(frozen=True, slots=True)
class ApplicationModules:
    database: DatabaseModules
    bot: Bot
    dispatcher: Dispatcher

    async def close(self) -> None:
        await self.database.close()
        await self.bot.session.close()


class Runtime:
    def __init__(
        self,
        settings: Settings,
        bot_factory: Callable[[str], Bot] = Bot,
        engine_factory: Callable[[str], AsyncEngine] = _create_engine,
    ) -> None:
        self._settings = settings
        self._bot_factory = bot_factory
        self._engine_factory = engine_factory

    async def run_retention(self) -> RetentionResult:
        modules = self._database_modules()
        try:
            await modules.prepare(self._settings.local_database_url)
            return await modules.retention.run()
        finally:
            await modules.close()

    async def run_sync(self) -> SyncDrainResult:
        modules = self._database_modules()
        try:
            await modules.prepare(self._settings.local_database_url)
            if modules.sync is None:
                raise ValueError("Local storage mode is required for synchronization")
            return await modules.sync.drain()
        finally:
            await modules.close()

    async def set_webhook(self) -> None:
        bot = self._bot()
        try:
            await self._configure_webhook(bot)
        finally:
            await bot.session.close()

    async def poll(self) -> None:
        modules = self._application_modules()
        health = RetentionHealth()
        retention_task: asyncio.Task[None] | None = None
        sync_task: asyncio.Task[None] | None = None
        try:
            await modules.database.prepare(self._settings.local_database_url)
            await modules.bot.delete_webhook(drop_pending_updates=False)
            await self._set_commands(modules.bot)
            if self._settings.background_retention_enabled:
                retention_task = asyncio.create_task(modules.database.retention.run_forever(health))
            if modules.database.sync is not None and modules.database.sync_health is not None:
                sync_task = asyncio.create_task(
                    modules.database.sync.run_forever(modules.database.sync_health)
                )
            await modules.dispatcher.start_polling(
                modules.bot,
                close_bot_session=False,
            )
        finally:
            if retention_task is not None:
                retention_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await retention_task
            if sync_task is not None:
                sync_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sync_task
            await modules.close()

    def webhook_app(self) -> web.Application:
        if not self._settings.webhook_enabled or self._settings.webhook_secret is None:
            raise ValueError("Webhook configuration is required")
        modules = self._application_modules()
        health = RetentionHealth()

        async def live(_: web.Request) -> web.Response:
            return web.json_response({"status": "ok"})

        async def ready(_: web.Request) -> web.Response:
            if not health.healthy:
                return web.json_response(
                    {"status": "degraded", "retention": "retrying"},
                    status=503,
                )
            readiness_engine = modules.database.local_engine or modules.database.engine
            if not await self.database_is_ready(readiness_engine):
                return web.json_response(
                    {"status": "unready", "database": "unavailable"},
                    status=503,
                )
            if (
                modules.database.sync_health is not None
                and not modules.database.sync_health.healthy
            ):
                return web.json_response({"status": "degraded", "synchronization": "retrying"})
            if (
                modules.database.sync_health is not None
                and modules.database.sync_health.revision_mismatches
            ):
                return web.json_response(
                    {
                        "status": "degraded",
                        "revision_mismatches": (modules.database.sync_health.revision_mismatches),
                    }
                )
            return web.json_response({"status": "ok"})

        async def prepare_storage(_: web.Application) -> None:
            await modules.database.prepare(self._settings.local_database_url)

        async def configure_webhook(_: web.Application) -> None:
            await self._configure_webhook(modules.bot)

        async def retention_context(_: web.Application) -> AsyncIterator[None]:
            task = asyncio.create_task(modules.database.retention.run_forever(health))
            yield
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        async def cleanup(_: web.Application) -> None:
            await modules.close()

        app = web.Application()
        app.router.add_get("/live", live)
        app.router.add_get("/health", ready)
        app.on_startup.append(prepare_storage)
        if self._settings.configure_webhook_on_startup:
            app.on_startup.append(configure_webhook)
        if self._settings.background_retention_enabled:
            app.cleanup_ctx.append(retention_context)
        sync_service = modules.database.sync
        sync_state = modules.database.sync_health
        if sync_service is not None and sync_state is not None:

            async def sync_context(_: web.Application) -> AsyncIterator[None]:
                task = asyncio.create_task(sync_service.run_forever(sync_state))
                yield
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            app.cleanup_ctx.append(sync_context)
        app.on_cleanup.append(cleanup)
        SimpleRequestHandler(
            dispatcher=modules.dispatcher,
            bot=modules.bot,
            secret_token=self._settings.webhook_secret.get_secret_value(),
        ).register(app, path=_WEBHOOK_PATH)
        setup_application(app, modules.dispatcher, bot=modules.bot)
        return app

    async def database_is_ready(self, engine: AsyncEngine) -> bool:
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            _logger.warning("Database readiness probe failed")
            return False
        return True

    def serve(self) -> None:
        if self._settings.webhook_enabled:
            web.run_app(self.webhook_app(), port=self._settings.port)
        else:
            asyncio.run(self.poll())

    def _database_modules(self) -> DatabaseModules:
        engine = self._engine_factory(self._settings.database_url)
        sessions = session_factory(engine)
        questionnaires = load_bundled_questionnaires()
        references = UserReferences(self._settings.analytics_pepper.get_secret_value())
        remote_assessments = SqlAssessmentRepository(sessions)
        remote_analytics_repository = SqlAnalyticsRepository(sessions)
        remote_rights = SqlUserDataRightsRepository(sessions)
        assessments_repository: AssessmentRepository
        rights_repository: UserDataRightsRepository
        analytics_repository: AnalyticsRepository
        local_engine: AsyncEngine | None = None
        sync: SyncService | None = None
        sync_health: SyncHealth | None = None
        bootstrap: LocalBootstrapService | None = None
        if self._settings.local_storage_enabled:
            local_engine = create_local_engine(self._settings.local_database_url or "")
            local_sessions = session_factory(local_engine)
            outbox = SyncOutboxRepository(local_sessions)
            storage_guard = LocalStorageGuard(
                self._settings.local_database_url or "",
                outbox.oldest_pending,
            )
            local_analytics_repository = SqlAnalyticsRepository(local_sessions, outbox=outbox)
            authoritative = self._settings.storage_mode == ReplicationMode.AUTHORITATIVE.value
            transition_events = (
                LocalAssessmentEventWriter(
                    local_analytics_repository,
                    references,
                )
                if authoritative
                else None
            )
            local_assessments = SqlAssessmentRepository(
                local_sessions,
                outbox=outbox,
                transition_events=transition_events,
                storage_label="local",
            )
            local_rights = SqlUserDataRightsRepository(local_sessions, outbox=outbox)
            sync_health = SyncHealth()
            assessments_repository = LocalFirstAssessmentRepository(
                local_assessments,
                remote_assessments,
                outbox,
                ReplicationMode(self._settings.storage_mode),
                storage_guard,
                sync_health,
            )
            rights_repository = LocalFirstUserDataRightsRepository(
                local_rights,
                remote_rights,
                ReplicationMode(self._settings.storage_mode),
            )
            analytics_repository = (
                AuthoritativeAnalyticsRepository(
                    local_analytics_repository,
                    remote_analytics_repository,
                    storage_guard,
                )
                if authoritative
                else MirroringAnalyticsRepository(
                    local_analytics_repository,
                    remote_analytics_repository,
                )
            )
            sync = SyncService(
                outbox,
                local_assessments,
                local_analytics_repository,
                remote_assessments,
                remote_analytics_repository,
                references,
                local_rights,
                remote_rights,
            )
            if self._settings.storage_mode == ReplicationMode.SHADOW.value:
                bootstrap = LocalBootstrapService(
                    local_assessments,
                    remote_assessments,
                )
        else:
            assessments_repository = RetryingAssessmentRepository(remote_assessments)
            rights_repository = RetryingUserDataRightsRepository(remote_rights)
            analytics_repository = RetryingAnalyticsRepository(remote_analytics_repository)
        assessments = AssessmentService(assessments_repository, questionnaires)
        analytics = AnalyticsService(analytics_repository, references)
        retention = DataRetention(assessments, analytics)
        return DatabaseModules(
            engine,
            sessions,
            local_engine,
            questionnaires,
            references,
            assessments,
            analytics,
            retention,
            sync,
            sync_health,
            rights_repository,
            bootstrap,
        )

    def _application_modules(self) -> ApplicationModules:
        database = self._database_modules()
        data_rights = UserDataRights(
            database.rights_repository,
            database.user_references,
        )
        interaction = AssessmentInteraction(
            database.assessments,
            database.analytics,
            database.questionnaires,
            transition_analytics_persisted=(
                self._settings.storage_mode == ReplicationMode.AUTHORITATIVE.value
            ),
        )
        dispatcher = Dispatcher(
            interaction=interaction,
            analytics=database.analytics,
            data_rights=data_rights,
            privacy_contact=self._settings.privacy_contact,
            admin_user_id=self._settings.admin_user_id,
            action_locks=UserActionLocks(),
        )
        router = create_router()
        rate_limit = RateLimitMiddleware()
        router.message.middleware(rate_limit)
        router.callback_query.middleware(rate_limit)
        dispatcher.include_router(router)
        return ApplicationModules(database, self._bot(), dispatcher)

    def _bot(self) -> Bot:
        return self._bot_factory(self._settings.bot_token.get_secret_value())

    async def _configure_webhook(self, bot: Bot) -> None:
        if not self._settings.webhook_enabled or self._settings.webhook_secret is None:
            raise ValueError("Webhook configuration is required")
        await self._set_commands(bot)
        base_url = (self._settings.webhook_base_url or "").rstrip("/")
        await bot.set_webhook(
            base_url + _WEBHOOK_PATH,
            secret_token=self._settings.webhook_secret.get_secret_value(),
        )

    @staticmethod
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
