from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import create_async_engine

from ocean_bot.application.analytics import AnalyticsService
from ocean_bot.application.data_rights import UserReferences
from ocean_bot.infrastructure.database import Base, SqlAnalyticsRepository, session_factory


async def test_analytics_are_pseudonymous_aggregated_and_purgeable() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repository = SqlAnalyticsRepository(session_factory(engine))
    service = AnalyticsService(repository, UserReferences("test-pepper"))
    now = datetime(2026, 4, 2, tzinfo=UTC)

    await service.record(42, "assessment_started", now - timedelta(days=91))
    await service.record(42, "assessment_completed", now)
    await service.record(99, "assessment_completed", now)

    assert await service.stats() == {
        "assessment_completed": 2,
        "assessment_started": 1,
    }
    assert await service.purge_before(now - timedelta(days=90)) == 1
    assert await service.stats() == {
        "assessment_completed": 2,
        "assessment_started": 1,
    }
    await engine.dispose()
