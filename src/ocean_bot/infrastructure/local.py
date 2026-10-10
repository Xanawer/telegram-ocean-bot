from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ocean_bot.application.assessments import StorageUnavailable, SyncBacklogExceeded


class LocalStorageGuard:
    def __init__(
        self,
        database_url: str,
        oldest_pending: Callable[[], Awaitable[datetime | None]],
    ) -> None:
        path = database_url.removeprefix("sqlite+aiosqlite:///")
        self._volume_path = Path(path).parent if path else Path.cwd()
        self._oldest_pending = oldest_pending

    async def ensure_writable(self, *, starting_new_assessment: bool = False) -> None:
        try:
            usage = shutil.disk_usage(self._volume_path)
        except OSError as error:
            raise StorageUnavailable(
                "Local assessment storage is unavailable; please try again later"
            ) from error
        if usage.used / usage.total >= 0.9:
            raise StorageUnavailable("Local assessment storage is full; please try again later")
        if starting_new_assessment:
            oldest = await self._oldest_pending()
            if oldest is not None:
                if oldest.tzinfo is None:
                    oldest = oldest.replace(tzinfo=UTC)
                if datetime.now(UTC) - oldest >= timedelta(hours=24):
                    raise SyncBacklogExceeded(
                        "Assessment synchronization is delayed; please try again later"
                    )


def create_local_engine(database_url: str) -> AsyncEngine:
    if not database_url.startswith("sqlite+aiosqlite:///"):
        raise ValueError("Local storage must use sqlite+aiosqlite")
    engine = create_async_engine(database_url, pool_pre_ping=False, hide_parameters=True)

    @event.listens_for(engine.sync_engine, "connect")
    def configure_connection(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    return engine
