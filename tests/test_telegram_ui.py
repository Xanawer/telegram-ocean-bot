from datetime import UTC, datetime

from ocean_bot.application.assessments import AssessmentService
from ocean_bot.domain.questionnaire import Item, Questionnaire, Trait
from ocean_bot.infrastructure.memory import InMemoryAssessmentRepository
from ocean_bot.telegram.ui import render_question, render_results


async def test_question_message_has_progress_scale_and_bounded_callbacks() -> None:
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (
            Item("e1", "I start conversations.", Trait.EXTRAVERSION, False),
            Item("e2", "I keep in the background.", Trait.EXTRAVERSION, True),
        ),
    )
    service = AssessmentService(InMemoryAssessmentRepository(), {"quick": questionnaire})
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await service.accept_terms(42, "en", now)
    view = await service.start(42, "quick", now)

    rendered = render_question(questionnaire, view)

    assert "Question 1 of 2" in rendered.text
    assert "1 Strongly disagree" in rendered.text
    answer_buttons = rendered.reply_markup.inline_keyboard[0]
    assert [button.text for button in answer_buttons] == ["1", "2", "3", "4", "5"]
    assert all(
        button.callback_data and len(button.callback_data.encode()) <= 64
        for button in answer_buttons
    )


async def test_completed_result_is_self_contained_and_non_clinical() -> None:
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (Item("e1", "First", Trait.EXTRAVERSION, False),),
    )
    service = AssessmentService(InMemoryAssessmentRepository(), {"quick": questionnaire})
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await service.accept_terms(42, "en", now)
    started = await service.start(42, "quick", now)
    completed = await service.answer(42, started.id, "e1", 5, now)

    text = render_results(questionnaire, completed)

    assert "Extraversion: 5.00 / 5" in text
    assert "Population comparison: unavailable" in text
    assert "self-reflection" in text
    assert "not a clinical assessment" in text
