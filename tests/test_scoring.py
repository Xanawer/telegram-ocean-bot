from ocean_bot.domain.questionnaire import Item, Questionnaire, Trait
from ocean_bot.domain.scoring import score_questionnaire


def test_scores_positive_and_reverse_keyed_items_from_fixed_answers() -> None:
    questionnaire = Questionnaire(
        id="worked-example",
        version="1",
        title="Worked example",
        items=(
            Item(id="e1", text="Positive E", trait=Trait.EXTRAVERSION, reverse=False),
            Item(id="e2", text="Reverse E", trait=Trait.EXTRAVERSION, reverse=True),
            Item(id="a1", text="Positive A", trait=Trait.AGREEABLENESS, reverse=False),
            Item(id="a2", text="Reverse A", trait=Trait.AGREEABLENESS, reverse=True),
        ),
    )

    result = score_questionnaire(
        questionnaire,
        {"e1": 5, "e2": 2, "a1": 1, "a2": 4},
    )

    assert result.raw_scores == {
        Trait.EXTRAVERSION: 4.5,
        Trait.AGREEABLENESS: 1.5,
    }
    assert result.scored_answers == {"e1": 5, "e2": 4, "a1": 1, "a2": 2}
