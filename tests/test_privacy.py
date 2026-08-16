from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import create_async_engine

from ocean_bot.application.analytics import AnalyticsService
from ocean_bot.application.assessments import AssessmentService
from ocean_bot.application.data_rights import UserDataRights, UserReferences
from ocean_bot.domain.questionnaire import Item, Questionnaire, Trait
from ocean_bot.infrastructure.database import (
    Base,
    SqlAnalyticsRepository,
    SqlAssessmentRepository,
    SqlUserDataRightsRepository,
    session_factory,
)


async def test_complete_deletion_is_atomic_and_preserves_only_anonymous_counters() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = session_factory(engine)
    references = UserReferences("long-test-pepper")
    analytics = AnalyticsService(SqlAnalyticsRepository(sessions), references)
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
    data_rights = UserDataRights(SqlUserDataRightsRepository(sessions), references)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await assessments.accept_terms(42, "en", now)
    started = await assessments.start(42, "quick", now)
    await assessments.answer(42, started.id, "e1", 5, now)
    await analytics.record(42, "assessment_completed", now)

    exported = (await data_rights.export_user(42)).document
    assert exported["user"]["language_code"] == "en"
    assert exported["assessments"][0]["answers"] == {"e1": 5}
    assert exported["analytics"][0]["name"] == "assessment_completed"
    assert "42" not in str(exported["analytics"])

    await data_rights.delete_user(42)

    assert (await data_rights.export_user(42)).document == {
        "user": None,
        "assessments": [],
        "analytics": [],
    }
    assert await analytics.stats() == {"assessment_completed": 1}
    await engine.dispose()
