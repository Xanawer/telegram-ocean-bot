from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ocean_bot.application.assessments import AssessmentService
from ocean_bot.domain.questionnaire import Item, Questionnaire, Trait
from ocean_bot.infrastructure.memory import InMemoryAssessmentRepository
from ocean_bot.telegram.handlers import (
    admin_stats_command,
    answer_question,
    results_command,
    start_command,
)


async def test_start_handler_requests_age_and_privacy_consent() -> None:
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42, language_code="en"),
        answer=AsyncMock(),
    )
    service = AssessmentService(InMemoryAssessmentRepository(), {})

    await start_command(message, service)

    text = message.answer.await_args.args[0]
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert "13 or older" in text
    assert "analytics" in text
    assert keyboard.inline_keyboard[0][0].callback_data == "consent:accept"
    assert keyboard.inline_keyboard[0][1].callback_data == "consent:decline"


async def test_stale_answer_callback_gets_an_inactive_alert() -> None:
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (
            Item("e1", "First", Trait.EXTRAVERSION, False),
            Item("e2", "Second", Trait.EXTRAVERSION, False),
        ),
    )
    service = AssessmentService(InMemoryAssessmentRepository(), {"quick": questionnaire})
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await service.accept_terms(42, "en", now)
    started = await service.start(42, "quick", now)
    await service.answer(42, started.id, "e1", 5, now)
    callback = SimpleNamespace(
        data=f"a:{started.id}:e1:4",
        from_user=SimpleNamespace(id=42),
        message=None,
        answer=AsyncMock(),
    )
    analytics = SimpleNamespace(record=AsyncMock())

    await answer_question(callback, service, analytics, {"quick": questionnaire})

    assert callback.answer.await_args.kwargs["show_alert"] is True
    analytics.record.assert_not_awaited()


async def test_admin_stats_are_allowlisted() -> None:
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=99, language_code="en"),
        answer=AsyncMock(),
    )
    analytics = SimpleNamespace(stats=AsyncMock(return_value={"assessment_completed": 2}))

    await admin_stats_command(message, admin_user_id=42, analytics=analytics)

    message.answer.assert_awaited_once_with("Command not available.")
    analytics.stats.assert_not_awaited()


async def test_results_history_exposes_delete_for_older_results() -> None:
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (Item("e1", "First", Trait.EXTRAVERSION, False),),
    )
    service = AssessmentService(InMemoryAssessmentRepository(), {"quick": questionnaire})
    first_at = datetime(2026, 1, 1, tzinfo=UTC)
    second_at = datetime(2026, 1, 2, tzinfo=UTC)
    await service.accept_terms(42, "en", first_at)
    first = await service.start(42, "quick", first_at)
    await service.answer(42, first.id, "e1", 5, first_at)
    second = await service.start(42, "quick", second_at)
    await service.answer(42, second.id, "e1", 4, second_at)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42, language_code="en"),
        answer=AsyncMock(),
    )

    await results_command(message, service, {"quick": questionnaire})

    assert message.answer.await_count == 2
    older_keyboard = message.answer.await_args_list[1].kwargs["reply_markup"]
    assert older_keyboard.inline_keyboard[0][1].callback_data == f"result:delete:{first.id}"
