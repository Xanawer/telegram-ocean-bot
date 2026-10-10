from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ocean_bot.application.assessments import AssessmentService
from ocean_bot.application.interactions import AssessmentInteraction, TextFormat
from ocean_bot.domain.catalog import load_bundled_questionnaires
from ocean_bot.domain.questionnaire import Item, Questionnaire, Trait
from ocean_bot.infrastructure.memory import InMemoryAssessmentRepository
from ocean_bot.telegram.callbacks import encode_callback


def _interaction(questionnaire: Questionnaire) -> AssessmentInteraction:
    assessments = AssessmentService(
        InMemoryAssessmentRepository(), {questionnaire.id: questionnaire}
    )
    analytics = SimpleNamespace(record=AsyncMock())
    return AssessmentInteraction(
        assessments,
        analytics,  # type: ignore[arg-type]
        {questionnaire.id: questionnaire},
    )


async def test_question_outcome_has_progress_scale_and_bounded_callbacks() -> None:
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (
            Item("e1", "I start conversations.", Trait.EXTRAVERSION, False),
            Item("e2", "I keep in the background.", Trait.EXTRAVERSION, True),
        ),
    )
    interaction = _interaction(questionnaire)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await interaction.accept_consent(42, "en", now)

    outcome = await interaction.choose_mode(42, "quick", now)
    reply = outcome.replies[0]

    assert reply.text_format is TextFormat.HTML
    assert "<b>Question 1 of 2</b>" in reply.text
    assert "<code>█████░░░░░</code>" in reply.text
    assert "<b>I start conversations.</b>" in reply.text
    assert "<code>1</code> Strongly disagree" in reply.text
    assert "<code>5</code> Strongly agree" in reply.text
    assert [button.text for button in reply.buttons[0]] == ["1", "2", "3", "4", "5"]
    assert [button.text for button in reply.buttons[1]] == ["Cancel"]
    assert all(len(encode_callback(button.intent).encode()) <= 64 for button in reply.buttons[0])

    second = await interaction.answer(
        42,
        reply.buttons[0][2].intent.values[0],
        reply.buttons[0][2].intent.values[1],
        3,
        now,
    )

    assert [button.text for button in second.replies[0].buttons[1]] == ["← Back", "Cancel"]


async def test_completed_result_is_self_contained_and_non_clinical() -> None:
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (Item("e1", "First", Trait.EXTRAVERSION, False, "Friendliness"),),
    )
    interaction = _interaction(questionnaire)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await interaction.accept_consent(42, "en", now)
    started = await interaction.choose_mode(42, "quick", now)
    answer_intent = started.replies[0].buttons[0][-1].intent

    completed = await interaction.answer(
        42,
        answer_intent.values[0],
        answer_intent.values[1],
        int(answer_intent.values[2]),
        now,
    )
    reply = completed.replies[0]
    text = reply.text

    assert reply.text_format is TextFormat.HTML
    assert "<b>Quick results</b>" in text
    assert "<i>1 Jan 2026</i>" in text
    assert "<b>Extraversion</b>  5.00 / 5" in text
    assert "<code>██████████</code>" in text
    assert "• Friendliness: 5.00 / 5" in text
    assert "Population percentiles are not shown" in text
    assert "<b>Questions for reflection</b>" in text
    assert "self-reflection" in text
    assert "not a clinical assessment" in text.lower()


async def test_mode_picker_advertises_the_ipip_neo_120_suite() -> None:
    questionnaires = load_bundled_questionnaires()
    assessments = AssessmentService(InMemoryAssessmentRepository(), questionnaires)
    interaction = AssessmentInteraction(
        assessments,
        SimpleNamespace(record=AsyncMock()),  # type: ignore[arg-type]
        questionnaires,
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    outcome = await interaction.accept_consent(42, "en", now)
    buttons = [button for row in outcome.replies[0].buttons for button in row]

    assert [button.intent.values for button in buttons] == [
        ("mini-ipip-20",),
        ("ipip-50",),
        ("ipip-120",),
    ]
    assert "120 questions, 18-25 min" in buttons[-1].text

    started = await interaction.choose_mode(42, "ipip-120", now)
    assert "<code>1</code> Very inaccurate" in started.replies[0].text
    assert "<code>5</code> Very accurate" in started.replies[0].text
