from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from ocean_bot.application.assessments import AssessmentService, StaleInput
from ocean_bot.domain.questionnaire import Item, Questionnaire, Trait
from ocean_bot.infrastructure.database import Base, SqlAssessmentRepository, session_factory


@pytest.mark.asyncio
async def test_sql_repository_persists_history_and_cascades_user_deletion() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = session_factory(engine)
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (Item("e1", "First", Trait.EXTRAVERSION, False),),
    )
    service = AssessmentService(SqlAssessmentRepository(sessions), {"quick": questionnaire})
    now = datetime(2026, 1, 1, tzinfo=UTC)

    await service.accept_terms(42, "en", now)
    started = await service.start(42, "quick", now)

    reloaded_active = AssessmentService(SqlAssessmentRepository(sessions), {"quick": questionnaire})
    assert await reloaded_active.resume(42, datetime(2026, 1, 2, tzinfo=UTC)) is not None

    await service.answer(42, started.id, "e1", 5, now)
    reloaded = AssessmentService(SqlAssessmentRepository(sessions), {"quick": questionnaire})
    assert (await reloaded.history(42))[0].scores == {Trait.EXTRAVERSION: 5.0}

    await reloaded.delete_user(42)
    assert await reloaded.export_user(42) == {"user": None, "assessments": []}
    await engine.dispose()


@pytest.mark.asyncio
async def test_sql_repository_rejects_stale_concurrent_write() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = session_factory(engine)
    repository = SqlAssessmentRepository(sessions)
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (Item("e1", "First", Trait.EXTRAVERSION, False),),
    )
    service = AssessmentService(repository, {"quick": questionnaire})
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await service.accept_terms(42, "en", now)
    started = await service.start(42, "quick", now)
    first = await repository.get_assessment(started.id)
    second = await repository.get_assessment(started.id)
    assert first is not None and second is not None
    first.answers["e1"] = 5
    second.answers["e1"] = 4

    await repository.save_assessment(first)
    with pytest.raises(StaleInput, match="changed"):
        await repository.save_assessment(second)

    await engine.dispose()
