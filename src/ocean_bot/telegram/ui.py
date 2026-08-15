from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ocean_bot.domain.assessment import AssessmentStatus, AssessmentView
from ocean_bot.domain.questionnaire import Questionnaire, Trait


@dataclass(frozen=True, slots=True)
class RenderedQuestion:
    text: str
    reply_markup: InlineKeyboardMarkup


_TRAIT_LABELS = {
    Trait.OPENNESS: "Openness",
    Trait.CONSCIENTIOUSNESS: "Conscientiousness",
    Trait.EXTRAVERSION: "Extraversion",
    Trait.AGREEABLENESS: "Agreeableness",
    Trait.NEUROTICISM: "Neuroticism",
}

_REFLECTIONS = {
    Trait.OPENNESS: "How much do you seek new ideas and experiences?",
    Trait.CONSCIENTIOUSNESS: "How do structure and follow-through show up in your routines?",
    Trait.EXTRAVERSION: "Which social settings tend to give you energy?",
    Trait.AGREEABLENESS: "How do you balance cooperation with your own boundaries?",
    Trait.NEUROTICISM: "What situations tend to intensify or settle your emotions?",
}


def render_question(
    questionnaire: Questionnaire,
    assessment: AssessmentView,
) -> RenderedQuestion:
    if assessment.status is not AssessmentStatus.ACTIVE or assessment.current_item_id is None:
        raise ValueError("Only an active assessment can render a question")
    item = next(item for item in questionnaire.items if item.id == assessment.current_item_id)
    progress = assessment.answered_count + 1
    text = (
        f"Question {progress} of {assessment.total_items}\n\n"
        f"{item.text}\n\n"
        "1 Strongly disagree | 2 Disagree | 3 Neutral\n"
        "4 Agree | 5 Strongly agree"
    )
    answers = [
        InlineKeyboardButton(
            text=str(value),
            callback_data=f"a:{assessment.id}:{item.id}:{value}",
        )
        for value in range(1, 6)
    ]
    navigation = [
        InlineKeyboardButton(text="Back", callback_data=f"b:{assessment.id}"),
        InlineKeyboardButton(text="Cancel", callback_data=f"c:{assessment.id}"),
    ]
    return RenderedQuestion(text, InlineKeyboardMarkup(inline_keyboard=[answers, navigation]))


def render_results(questionnaire: Questionnaire, assessment: AssessmentView) -> str:
    if assessment.status is not AssessmentStatus.COMPLETED:
        raise ValueError("Only completed assessments have results")
    score_lines = [
        f"{_TRAIT_LABELS[trait]}: {assessment.scores[trait]:.2f} / 5"
        for trait in Trait
        if trait in assessment.scores
    ]
    prompts = [f"- {_REFLECTIONS[trait]}" for trait in Trait if trait in assessment.scores][:2]
    completed = assessment.completed_at.date().isoformat() if assessment.completed_at else ""
    return "\n".join(
        [
            f"{questionnaire.title} results",
            completed,
            "",
            *score_lines,
            "",
            (
                "Population comparison: unavailable until a compatible documented "
                "benchmark is installed."
            ),
            "",
            "Reflection prompts",
            *prompts,
            "",
            "For entertainment and self-reflection only; this is not a clinical assessment.",
        ]
    )
