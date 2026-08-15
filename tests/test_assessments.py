from datetime import UTC, datetime

import pytest

from ocean_bot.application.assessments import AssessmentService, StaleInput
from ocean_bot.domain.assessment import AssessmentStatus
from ocean_bot.domain.questionnaire import Item, Questionnaire, Trait
from ocean_bot.infrastructure.memory import InMemoryAssessmentRepository


@pytest.fixture
def questionnaire() -> Questionnaire:
    return Questionnaire(
        id="quick",
        version="1",
        title="Quick",
        items=(
            Item("e1", "First", Trait.EXTRAVERSION, False),
            Item("e2", "Second", Trait.EXTRAVERSION, True),
        ),
    )


@pytest.fixture
def service(questionnaire: Questionnaire) -> AssessmentService:
    return AssessmentService(InMemoryAssessmentRepository(), {questionnaire.id: questionnaire})


@pytest.mark.asyncio
async def test_user_can_consent_start_and_complete_an_assessment(
    service: AssessmentService,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await service.accept_terms(telegram_user_id=42, language_code="en", now=now)

    started = await service.start(telegram_user_id=42, questionnaire_id="quick", now=now)
    after_first = await service.answer(42, started.id, "e1", 5, now)
    completed = await service.answer(42, started.id, "e2", 2, now)

    assert after_first.current_item_id == "e2"
    assert completed.status is AssessmentStatus.COMPLETED
    assert completed.scores == {Trait.EXTRAVERSION: 4.5}
    assert len(await service.history(42)) == 1


@pytest.mark.asyncio
async def test_duplicate_answer_is_idempotent_but_changed_stale_answer_is_rejected(
    service: AssessmentService,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await service.accept_terms(42, "en", now)
    started = await service.start(42, "quick", now)
    expected = await service.answer(42, started.id, "e1", 5, now)

    duplicate = await service.answer(42, started.id, "e1", 5, now)

    assert duplicate == expected
    with pytest.raises(StaleInput):
        await service.answer(42, started.id, "e1", 4, now)


@pytest.mark.asyncio
async def test_user_can_resume_go_back_cancel_export_and_delete(
    service: AssessmentService,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await service.accept_terms(42, "en", now)
    started = await service.start(42, "quick", now)
    await service.answer(42, started.id, "e1", 5, now)

    resumed = await service.resume(42, now)
    backed_up = await service.back(42, started.id, now)
    export = await service.export_user(42)

    assert resumed is not None and resumed.current_item_id == "e2"
    assert backed_up.current_item_id == "e1"
    assert export["user"]["language_code"] == "en"
    assert export["assessments"][0]["answers"] == {}

    cancelled = await service.cancel(42, started.id, now)
    assert cancelled.status is AssessmentStatus.CANCELLED
    assert await service.resume(42, now) is None

    await service.delete_user(42)
    assert await service.export_user(42) == {"user": None, "assessments": []}


@pytest.mark.asyncio
async def test_incomplete_assessments_expire_after_thirty_days(
    service: AssessmentService,
) -> None:
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    await service.accept_terms(42, "en", started_at)
    started = await service.start(42, "quick", started_at)

    expired_count = await service.expire_incomplete(datetime(2026, 2, 1, tzinfo=UTC))

    assert expired_count == 1
    assert (await service.get(42, started.id)).status is AssessmentStatus.EXPIRED


@pytest.mark.asyncio
async def test_user_can_delete_one_completed_assessment(
    service: AssessmentService,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await service.accept_terms(42, "en", now)
    started = await service.start(42, "quick", now)
    await service.answer(42, started.id, "e1", 5, now)
    await service.answer(42, started.id, "e2", 2, now)

    await service.delete_assessment(42, started.id)

    assert await service.history(42) == []


@pytest.mark.asyncio
async def test_expired_assessment_rejects_late_answer_without_cleanup_job(
    service: AssessmentService,
) -> None:
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    await service.accept_terms(42, "en", started_at)
    started = await service.start(42, "quick", started_at)

    with pytest.raises(StaleInput, match="expired"):
        await service.answer(42, started.id, "e1", 5, datetime(2026, 2, 1, tzinfo=UTC))

    assert (await service.get(42, started.id)).status is AssessmentStatus.EXPIRED


@pytest.mark.asyncio
async def test_individual_result_deletion_rejects_active_assessment(
    service: AssessmentService,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await service.accept_terms(42, "en", now)
    started = await service.start(42, "quick", now)

    with pytest.raises(StaleInput, match="completed"):
        await service.delete_assessment(42, started.id)
