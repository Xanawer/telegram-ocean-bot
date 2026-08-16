from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ocean_bot.application.assessments import AssessmentService
from ocean_bot.application.interactions import AssessmentInteraction, CallbackIntent
from ocean_bot.domain.questionnaire import Item, Questionnaire, Trait
from ocean_bot.infrastructure.memory import InMemoryAssessmentRepository
from ocean_bot.telegram.callbacks import decode_callback, encode_callback
from ocean_bot.telegram.handlers import admin_stats_command, handle_callback


def _interaction(
    questionnaires: dict[str, Questionnaire],
) -> tuple[AssessmentInteraction, SimpleNamespace]:
    analytics = SimpleNamespace(record=AsyncMock())
    interaction = AssessmentInteraction(
        AssessmentService(InMemoryAssessmentRepository(), questionnaires),
        analytics,  # type: ignore[arg-type]
        questionnaires,
    )
    return interaction, analytics


async def test_start_interaction_requests_age_and_privacy_consent() -> None:
    interaction, _ = _interaction({})

    outcome = await interaction.start(42)

    assert "13 or older" in outcome.replies[0].text
    assert "analytics" in outcome.replies[0].text
    assert encode_callback(outcome.replies[0].buttons[0][0].intent) == "consent:accept"
    assert encode_callback(outcome.replies[0].buttons[0][1].intent) == "consent:decline"


async def test_stale_answer_intent_gets_an_inactive_alert() -> None:
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (
            Item("e1", "First", Trait.EXTRAVERSION, False),
            Item("e2", "Second", Trait.EXTRAVERSION, False),
        ),
    )
    interaction, analytics = _interaction({"quick": questionnaire})
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await interaction.accept_consent(42, "en", now)
    started = await interaction.choose_mode(42, "quick", now)
    assessment_id = started.replies[0].buttons[0][0].intent.values[0]
    await interaction.answer(42, assessment_id, "e1", 5, now)
    analytics.record.reset_mock()

    outcome = await interaction.answer(42, assessment_id, "e1", 4, now)

    assert outcome.callback_alert is True
    analytics.record.assert_not_awaited()


async def test_telegram_adapter_decodes_stale_answer_and_acknowledges_alert() -> None:
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (
            Item("e1", "First", Trait.EXTRAVERSION, False),
            Item("e2", "Second", Trait.EXTRAVERSION, False),
        ),
    )
    interaction, analytics = _interaction({"quick": questionnaire})
    now = datetime.now(UTC)
    await interaction.accept_consent(42, "en", now)
    started = await interaction.choose_mode(42, "quick", now)
    assessment_id = started.replies[0].buttons[0][0].intent.values[0]
    await interaction.answer(42, assessment_id, "e1", 5, now)
    analytics.record.reset_mock()
    callback = SimpleNamespace(
        data=encode_callback(CallbackIntent("answer", (assessment_id, "e1", "4"))),
        from_user=SimpleNamespace(id=42, language_code="en"),
        message=None,
        answer=AsyncMock(),
    )

    await handle_callback(
        callback,
        interaction,
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    callback.answer.assert_awaited_once_with()
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


async def test_results_outcome_exposes_delete_for_older_results() -> None:
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (Item("e1", "First", Trait.EXTRAVERSION, False),),
    )
    interaction, _ = _interaction({"quick": questionnaire})
    first_at = datetime(2026, 1, 1, tzinfo=UTC)
    second_at = datetime(2026, 1, 2, tzinfo=UTC)
    await interaction.accept_consent(42, "en", first_at)
    first = await interaction.choose_mode(42, "quick", first_at)
    first_answer = first.replies[0].buttons[0][0].intent
    await interaction.answer(42, *first_answer.values[:2], 1, first_at)
    second = await interaction.choose_mode(42, "quick", second_at)
    second_answer = second.replies[0].buttons[0][0].intent
    await interaction.answer(42, *second_answer.values[:2], 1, second_at)

    outcome = await interaction.results(42)

    assert len(outcome.replies) == 2
    older_delete = outcome.replies[1].buttons[0][1].intent
    assert older_delete == CallbackIntent("result.delete", (first_answer.values[0],))


def test_callback_codec_round_trips_transport_formats() -> None:
    cases = {
        CallbackIntent("mode", ("quick",)): "mode:quick",
        CallbackIntent("answer", ("assessment", "e1", "5")): "a:assessment:e1:5",
        CallbackIntent("back", ("assessment",)): "b:assessment",
        CallbackIntent("cancel", ("assessment",)): "c:assessment",
        CallbackIntent("history.page", ("2",)): "history:page:2",
        CallbackIntent("result.view", ("assessment",)): "result:view:assessment",
        CallbackIntent("result.delete", ("assessment",)): "result:delete:assessment",
        CallbackIntent("result.confirm", ("assessment",)): "result:confirm:assessment",
        CallbackIntent("result.keep"): "result:keep",
        CallbackIntent("delete.confirm"): "delete:confirm",
        CallbackIntent("delete.cancel"): "delete:cancel",
    }

    assert {intent: encode_callback(intent) for intent in cases} == cases
    assert [decode_callback(value) for value in cases.values()] == list(cases)
