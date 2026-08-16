from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from ocean_bot.domain.questionnaire import Questionnaire, Trait
from ocean_bot.domain.scoring import score_questionnaire


class AssessmentStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class AssessmentStateError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class UserRecord:
    telegram_user_id: int
    language_code: str | None
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class Assessment:
    id: str
    telegram_user_id: int
    questionnaire_id: str
    questionnaire_version: str
    status: AssessmentStatus
    answers: tuple[tuple[str, int], ...]
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    scores: tuple[tuple[Trait, float], ...] = ()
    facet_scores: tuple[tuple[str, float], ...] = ()

    @classmethod
    def start(
        cls,
        assessment_id: str,
        telegram_user_id: int,
        questionnaire: Questionnaire,
        now: datetime,
    ) -> Assessment:
        return cls(
            id=assessment_id,
            telegram_user_id=telegram_user_id,
            questionnaire_id=questionnaire.id,
            questionnaire_version=questionnaire.version,
            status=AssessmentStatus.ACTIVE,
            answers=(),
            started_at=now,
            updated_at=now,
        )

    @property
    def answer_map(self) -> Mapping[str, int]:
        return MappingProxyType(dict(self.answers))

    @property
    def score_map(self) -> Mapping[Trait, float]:
        return MappingProxyType(dict(self.scores))

    @property
    def facet_score_map(self) -> Mapping[str, float]:
        return MappingProxyType(dict(self.facet_scores))

    def answer(
        self,
        questionnaire: Questionnaire,
        item_id: str,
        value: int,
        now: datetime,
    ) -> Assessment:
        self._require_questionnaire(questionnaire)
        answers = dict(self.answers)
        if item_id in answers:
            if answers[item_id] == value:
                return self
            raise AssessmentStateError("That answer has already been recorded")
        self._require_active()
        expected = questionnaire.items[len(self.answers)].id
        if item_id != expected:
            raise AssessmentStateError("This question is no longer active")
        if isinstance(value, bool) or not 1 <= value <= 5:
            raise ValueError("Answers must be integers from 1 to 5")

        next_answers = (*self.answers, (item_id, value))
        if len(next_answers) < len(questionnaire.items):
            return replace(self, answers=next_answers, updated_at=now)

        score = score_questionnaire(questionnaire, dict(next_answers))
        return replace(
            self,
            answers=next_answers,
            scores=tuple(score.raw_scores.items()),
            facet_scores=tuple(score.facet_scores.items()),
            status=AssessmentStatus.COMPLETED,
            updated_at=now,
            completed_at=now,
        )

    def back(self, questionnaire: Questionnaire, now: datetime) -> Assessment:
        self._require_questionnaire(questionnaire)
        self._require_active()
        if not self.answers:
            return self
        return replace(self, answers=self.answers[:-1], updated_at=now)

    def cancel(self, now: datetime) -> Assessment:
        self._require_active()
        return replace(self, status=AssessmentStatus.CANCELLED, updated_at=now)

    def expire(self, now: datetime) -> Assessment:
        self._require_active()
        return replace(self, status=AssessmentStatus.EXPIRED, updated_at=now)

    def view(self, questionnaire: Questionnaire) -> AssessmentView:
        self._require_questionnaire(questionnaire)
        current = (
            questionnaire.items[len(self.answers)].id
            if self.status is AssessmentStatus.ACTIVE
            and len(self.answers) < len(questionnaire.items)
            else None
        )
        return AssessmentView(
            id=self.id,
            questionnaire_id=self.questionnaire_id,
            questionnaire_version=self.questionnaire_version,
            status=self.status,
            current_item_id=current,
            answered_count=len(self.answers),
            total_items=len(questionnaire.items),
            scores=self.score_map,
            facet_scores=self.facet_score_map,
            started_at=self.started_at,
            completed_at=self.completed_at,
        )

    def _require_active(self) -> None:
        if self.status is not AssessmentStatus.ACTIVE:
            raise AssessmentStateError("This assessment is no longer active")

    def _require_questionnaire(self, questionnaire: Questionnaire) -> None:
        if (
            questionnaire.id != self.questionnaire_id
            or questionnaire.version != self.questionnaire_version
        ):
            raise AssessmentStateError("Questionnaire version is unavailable")


@dataclass(frozen=True, slots=True)
class AssessmentView:
    id: str
    questionnaire_id: str
    questionnaire_version: str
    status: AssessmentStatus
    current_item_id: str | None
    answered_count: int
    total_items: int
    scores: Mapping[Trait, float]
    facet_scores: Mapping[str, float]
    started_at: datetime
    completed_at: datetime | None
