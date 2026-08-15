from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from ocean_bot.application.assessments import ActiveAssessmentExists, StaleInput
from ocean_bot.domain.assessment import Assessment, AssessmentStatus, UserRecord


class InMemoryAssessmentRepository:
    def __init__(self) -> None:
        self._users: dict[int, UserRecord] = {}
        self._assessments: dict[str, Assessment] = {}

    async def save_user(self, user: UserRecord) -> None:
        self._users[user.telegram_user_id] = user

    async def get_user(self, telegram_user_id: int) -> UserRecord | None:
        return self._users.get(telegram_user_id)

    async def save_assessment(self, assessment: Assessment) -> None:
        existing = self._assessments.get(assessment.id)
        if existing is None:
            if any(
                candidate.telegram_user_id == assessment.telegram_user_id
                and candidate.status is AssessmentStatus.ACTIVE
                for candidate in self._assessments.values()
            ):
                raise ActiveAssessmentExists("An active assessment already exists")
            self._assessments[assessment.id] = deepcopy(assessment)
            return
        if existing.revision != assessment.revision:
            raise StaleInput("This assessment changed; use the latest message")
        assessment.revision += 1
        self._assessments[assessment.id] = deepcopy(assessment)

    async def get_assessment(self, assessment_id: str) -> Assessment | None:
        assessment = self._assessments.get(assessment_id)
        return deepcopy(assessment) if assessment is not None else None

    async def get_active(self, telegram_user_id: int) -> Assessment | None:
        assessment = next(
            (
                candidate
                for candidate in self._assessments.values()
                if candidate.telegram_user_id == telegram_user_id
                and candidate.status is AssessmentStatus.ACTIVE
            ),
            None,
        )
        return deepcopy(assessment) if assessment is not None else None

    async def list_completed(self, telegram_user_id: int) -> list[Assessment]:
        completed = [
            assessment
            for assessment in self._assessments.values()
            if assessment.telegram_user_id == telegram_user_id
            and assessment.status is AssessmentStatus.COMPLETED
        ]
        return deepcopy(
            sorted(
                completed,
                key=lambda assessment: assessment.completed_at or assessment.updated_at,
                reverse=True,
            )
        )

    async def list_all(self, telegram_user_id: int) -> list[Assessment]:
        return deepcopy(
            [
                assessment
                for assessment in self._assessments.values()
                if assessment.telegram_user_id == telegram_user_id
            ]
        )

    async def list_active_before(self, cutoff: datetime) -> list[Assessment]:
        return deepcopy(
            [
                assessment
                for assessment in self._assessments.values()
                if assessment.status is AssessmentStatus.ACTIVE and assessment.updated_at < cutoff
            ]
        )

    async def delete_assessment(self, assessment_id: str, telegram_user_id: int) -> None:
        assessment = self._assessments.get(assessment_id)
        if assessment is not None and assessment.telegram_user_id == telegram_user_id:
            del self._assessments[assessment_id]

    async def delete_user(self, telegram_user_id: int) -> None:
        self._users.pop(telegram_user_id, None)
        self._assessments = {
            assessment_id: assessment
            for assessment_id, assessment in self._assessments.items()
            if assessment.telegram_user_id != telegram_user_id
        }
