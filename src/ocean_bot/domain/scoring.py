from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from statistics import fmean
from types import MappingProxyType

from ocean_bot.domain.questionnaire import Questionnaire, Trait


@dataclass(frozen=True, slots=True)
class ScoreResult:
    raw_scores: Mapping[Trait, float]
    scored_answers: Mapping[str, int]
    facet_scores: Mapping[str, float] = MappingProxyType({})


def score_questionnaire(
    questionnaire: Questionnaire,
    answers: Mapping[str, int],
) -> ScoreResult:
    expected_ids = {item.id for item in questionnaire.items}
    supplied_ids = set(answers)
    if supplied_ids != expected_ids:
        missing = sorted(expected_ids - supplied_ids)
        unexpected = sorted(supplied_ids - expected_ids)
        raise ValueError(f"Answer set mismatch; missing={missing}, unexpected={unexpected}")

    scored_answers: dict[str, int] = {}
    scores_by_trait: dict[Trait, list[int]] = {}
    scores_by_facet: dict[str, list[int]] = {}
    for item in questionnaire.items:
        answer = answers[item.id]
        if isinstance(answer, bool) or not 1 <= answer <= 5:
            raise ValueError(f"Answer for {item.id} must be an integer from 1 to 5")
        scored = 6 - answer if item.reverse else answer
        scored_answers[item.id] = scored
        scores_by_trait.setdefault(item.trait, []).append(scored)
        if item.facet is not None:
            scores_by_facet.setdefault(item.facet, []).append(scored)

    raw_scores = {trait: fmean(values) for trait, values in scores_by_trait.items()}
    return ScoreResult(
        raw_scores=MappingProxyType(raw_scores),
        scored_answers=MappingProxyType(scored_answers),
        facet_scores=MappingProxyType(
            {facet: fmean(values) for facet, values in scores_by_facet.items()}
        ),
    )
