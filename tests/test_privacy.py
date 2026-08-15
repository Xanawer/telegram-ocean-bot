from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import create_async_engine

from ocean_bot.application.analytics import AnalyticsService
from ocean_bot.application.assessments import AssessmentService
from ocean_bot.application.privacy import PrivacyService
from ocean_bot.domain.questionnaire import Item, Questionnaire, Trait
from ocean_bot.infrastructure.database import (
    Base,
    SqlAnalyticsRepository,
    SqlAssessmentRepository,
    SqlPrivacyRepository,
    session_factory,
)


async def test_complete_deletion_is_atomic_and_preserves_only_anonymous_counters() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = session_factory(engine)
    analytics = AnalyticsService(SqlAnalyticsRepository(sessions), "long-test-pepper")
    assessments = AssessmentService(
        SqlAssessmentRepository(sessions),
        {
            "quick": Questionnaire(
                "quick",
                "1",
                "Quick",
                (Item("e1", "First", Trait.EXTRAVERSION, False),),
            )
        },
    )
    privacy = PrivacyService(SqlPrivacyRepository(sessions), analytics)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await assessments.accept_terms(42, "en", now)
    started = await assessments.start(42, "quick", now)
    await assessments.answer(42, started.id, "e1", 5, now)
    await analytics.record(42, "assessment_completed", now)

    await privacy.delete_user(42)

    assert await assessments.export_user(42) == {"user": None, "assessments": []}
    assert await analytics.export_user(42) == []
    assert await analytics.stats() == {"assessment_completed": 1}
    await engine.dispose()
