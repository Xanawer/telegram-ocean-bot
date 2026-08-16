from __future__ import annotations

from datetime import datetime

from ocean_bot.application.analytics import AnalyticsEvent
from ocean_bot.application.assessments import (
    ActiveAssessmentExists,
    StaleInput,
    StoredAssessment,
)
from ocean_bot.domain.assessment import Assessment, AssessmentStatus, UserRecord


class InMemoryAssessmentRepository:
    def __init__(self) -> None:
        self._users: dict[int, UserRecord] = {}
        self._assessments: dict[str, StoredAssessment] = {}

    async def save_user(self, user: UserRecord) -> None:
        self._users[user.telegram_user_id] = user

    async def get_user(self, telegram_user_id: int) -> UserRecord | None:
        return self._users.get(telegram_user_id)

    async def create_assessment(self, assessment: Assessment) -> StoredAssessment:
        if any(
            stored.assessment.telegram_user_id == assessment.telegram_user_id
            and stored.assessment.status is AssessmentStatus.ACTIVE
            for stored in self._assessments.values()
        ):
            raise ActiveAssessmentExists("An active assessment already exists")
        stored = StoredAssessment(assessment, token=0)
        self._assessments[assessment.id] = stored
        return stored

    async def save_assessment(
        self,
        stored: StoredAssessment,
        assessment: Assessment,
        event: AnalyticsEvent | None = None,
    ) -> StoredAssessment:
        current = self._assessments.get(assessment.id)
        if current is None or current.token != stored.token:
            raise StaleInput("This assessment changed; use the latest message")
        if not isinstance(stored.token, int):
            raise TypeError("Invalid in-memory persistence token")
        updated = StoredAssessment(assessment, token=stored.token + 1)
        self._assessments[assessment.id] = updated
        return updated

    async def get_assessment(self, assessment_id: str) -> StoredAssessment | None:
        return self._assessments.get(assessment_id)

    async def get_active(self, telegram_user_id: int) -> StoredAssessment | None:
        return next(
            (
                stored
                for stored in self._assessments.values()
                if stored.assessment.telegram_user_id == telegram_user_id
                and stored.assessment.status is AssessmentStatus.ACTIVE
            ),
            None,
        )

    async def list_completed(self, telegram_user_id: int) -> list[StoredAssessment]:
        completed = [
            stored
            for stored in self._assessments.values()
            if stored.assessment.telegram_user_id == telegram_user_id
            and stored.assessment.status is AssessmentStatus.COMPLETED
        ]
        return sorted(
            completed,
            key=lambda stored: stored.assessment.completed_at or stored.assessment.updated_at,
            reverse=True,
        )

    async def list_active_before(self, cutoff: datetime) -> list[StoredAssessment]:
        return [
            stored
            for stored in self._assessments.values()
            if stored.assessment.status is AssessmentStatus.ACTIVE
            and stored.assessment.updated_at < cutoff
        ]

    async def delete_assessment(self, assessment_id: str, telegram_user_id: int) -> None:
        stored = self._assessments.get(assessment_id)
        if stored is not None and stored.assessment.telegram_user_id == telegram_user_id:
            del self._assessments[assessment_id]
