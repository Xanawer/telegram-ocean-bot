from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from ocean_bot.domain.questionnaire import Trait


class AssessmentStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(slots=True)
class UserRecord:
    telegram_user_id: int
    language_code: str | None
    accepted_at: datetime


@dataclass(slots=True)
class Assessment:
    id: str
    telegram_user_id: int
    questionnaire_id: str
    questionnaire_version: str
    status: AssessmentStatus
    answers: dict[str, int]
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    scores: dict[Trait, float] = field(default_factory=dict)
    revision: int = 0


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
    started_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_assessment(
        cls,
        assessment: Assessment,
        item_ids: tuple[str, ...],
    ) -> AssessmentView:
        current = (
            item_ids[len(assessment.answers)] if len(assessment.answers) < len(item_ids) else None
        )
        return cls(
            id=assessment.id,
            questionnaire_id=assessment.questionnaire_id,
            questionnaire_version=assessment.questionnaire_version,
            status=assessment.status,
            current_item_id=current if assessment.status is AssessmentStatus.ACTIVE else None,
            answered_count=len(assessment.answers),
            total_items=len(item_ids),
            scores=MappingProxyType(dict(assessment.scores)),
            started_at=assessment.started_at,
            completed_at=assessment.completed_at,
        )
