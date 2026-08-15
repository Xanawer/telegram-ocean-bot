from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4

from ocean_bot.domain.assessment import (
    Assessment,
    AssessmentStatus,
    AssessmentView,
    UserRecord,
)
from ocean_bot.domain.questionnaire import Questionnaire
from ocean_bot.domain.scoring import score_questionnaire


class AssessmentError(Exception):
    """Base error safe for transport adapters to translate."""


class ConsentRequired(AssessmentError):
    pass


class AssessmentNotFound(AssessmentError):
    pass


class ActiveAssessmentExists(AssessmentError):
    pass


class StaleInput(AssessmentError):
    pass


class AssessmentRepository(Protocol):
    async def save_user(self, user: UserRecord) -> None: ...
    async def get_user(self, telegram_user_id: int) -> UserRecord | None: ...
    async def save_assessment(self, assessment: Assessment) -> None: ...
    async def get_assessment(self, assessment_id: str) -> Assessment | None: ...
    async def get_active(self, telegram_user_id: int) -> Assessment | None: ...
    async def list_completed(self, telegram_user_id: int) -> list[Assessment]: ...
    async def list_all(self, telegram_user_id: int) -> list[Assessment]: ...
    async def list_active_before(self, cutoff: datetime) -> list[Assessment]: ...
    async def delete_assessment(self, assessment_id: str, telegram_user_id: int) -> None: ...
    async def delete_user(self, telegram_user_id: int) -> None: ...


class AssessmentService:
    def __init__(
        self,
        repository: AssessmentRepository,
        questionnaires: Mapping[str, Questionnaire],
    ) -> None:
        self._repository = repository
        self._questionnaires = dict(questionnaires)

    async def accept_terms(
        self,
        telegram_user_id: int,
        language_code: str | None,
        now: datetime,
    ) -> None:
        await self._repository.save_user(UserRecord(telegram_user_id, language_code, now))

    async def has_consent(self, telegram_user_id: int) -> bool:
        return await self._repository.get_user(telegram_user_id) is not None

    async def start(
        self,
        telegram_user_id: int,
        questionnaire_id: str,
        now: datetime,
    ) -> AssessmentView:
        if await self._repository.get_user(telegram_user_id) is None:
            raise ConsentRequired("Accept the privacy notice before starting")
        if questionnaire_id not in self._questionnaires:
            raise AssessmentNotFound("Unknown questionnaire")
        active = await self._repository.get_active(telegram_user_id)
        if active is not None:
            raise ActiveAssessmentExists(active.id)
        questionnaire = self._questionnaires[questionnaire_id]
        assessment = Assessment(
            id=str(uuid4()),
            telegram_user_id=telegram_user_id,
            questionnaire_id=questionnaire.id,
            questionnaire_version=questionnaire.version,
            status=AssessmentStatus.ACTIVE,
            answers={},
            started_at=now,
            updated_at=now,
        )
        await self._repository.save_assessment(assessment)
        return self._view(assessment)

    async def answer(
        self,
        telegram_user_id: int,
        assessment_id: str,
        item_id: str,
        value: int,
        now: datetime,
    ) -> AssessmentView:
        assessment = await self._owned_assessment(telegram_user_id, assessment_id)
        await self._reject_expired(assessment, now)
        if item_id in assessment.answers:
            if assessment.answers[item_id] == value:
                return self._view(assessment)
            raise StaleInput("That answer has already been recorded")
        if assessment.status is not AssessmentStatus.ACTIVE:
            raise StaleInput("This assessment is no longer active")
        questionnaire = self._questionnaire_for(assessment)
        expected = questionnaire.items[len(assessment.answers)].id
        if item_id != expected:
            raise StaleInput("This question is no longer active")
        if isinstance(value, bool) or not 1 <= value <= 5:
            raise ValueError("Answers must be integers from 1 to 5")
        assessment.answers[item_id] = value
        assessment.updated_at = now
        if len(assessment.answers) == len(questionnaire.items):
            score = score_questionnaire(questionnaire, assessment.answers)
            assessment.scores = dict(score.raw_scores)
            assessment.status = AssessmentStatus.COMPLETED
            assessment.completed_at = now
        await self._repository.save_assessment(assessment)
        return self._view(assessment)

    async def history(self, telegram_user_id: int) -> list[AssessmentView]:
        assessments = await self._repository.list_completed(telegram_user_id)
        return [self._view(assessment) for assessment in assessments]

    async def get(self, telegram_user_id: int, assessment_id: str) -> AssessmentView:
        return self._view(await self._owned_assessment(telegram_user_id, assessment_id))

    async def resume(self, telegram_user_id: int, now: datetime) -> AssessmentView | None:
        active = await self._repository.get_active(telegram_user_id)
        if active is None:
            return None
        if now - active.updated_at >= timedelta(days=30):
            active.status = AssessmentStatus.EXPIRED
            active.updated_at = now
            await self._repository.save_assessment(active)
            return None
        return self._view(active)

    async def back(
        self,
        telegram_user_id: int,
        assessment_id: str,
        now: datetime,
    ) -> AssessmentView:
        assessment = await self._owned_assessment(telegram_user_id, assessment_id)
        await self._reject_expired(assessment, now)
        if assessment.status is not AssessmentStatus.ACTIVE:
            raise StaleInput("This assessment is no longer active")
        if assessment.answers:
            assessment.answers.pop(next(reversed(assessment.answers)))
            assessment.updated_at = now
            await self._repository.save_assessment(assessment)
        return self._view(assessment)

    async def cancel(
        self,
        telegram_user_id: int,
        assessment_id: str,
        now: datetime,
    ) -> AssessmentView:
        assessment = await self._owned_assessment(telegram_user_id, assessment_id)
        await self._reject_expired(assessment, now)
        if assessment.status is not AssessmentStatus.ACTIVE:
            raise StaleInput("This assessment is no longer active")
        assessment.status = AssessmentStatus.CANCELLED
        assessment.updated_at = now
        await self._repository.save_assessment(assessment)
        return self._view(assessment)

    async def expire_incomplete(self, now: datetime) -> int:
        assessments = await self._repository.list_active_before(now - timedelta(days=30))
        for assessment in assessments:
            assessment.status = AssessmentStatus.EXPIRED
            assessment.updated_at = now
            await self._repository.save_assessment(assessment)
        return len(assessments)

    async def export_user(self, telegram_user_id: int) -> dict[str, object]:
        user = await self._repository.get_user(telegram_user_id)
        assessments = await self._repository.list_all(telegram_user_id)
        user_data: dict[str, object] | None = None
        if user is not None:
            user_data = {
                "telegram_user_id": user.telegram_user_id,
                "language_code": user.language_code,
                "accepted_at": user.accepted_at.isoformat(),
            }
        return {
            "user": user_data,
            "assessments": [
                {
                    "id": assessment.id,
                    "questionnaire_id": assessment.questionnaire_id,
                    "questionnaire_version": assessment.questionnaire_version,
                    "status": assessment.status.value,
                    "answers": dict(assessment.answers),
                    "scores": {trait.value: score for trait, score in assessment.scores.items()},
                    "started_at": assessment.started_at.isoformat(),
                    "completed_at": (
                        assessment.completed_at.isoformat()
                        if assessment.completed_at is not None
                        else None
                    ),
                }
                for assessment in assessments
            ],
        }

    async def delete_assessment(self, telegram_user_id: int, assessment_id: str) -> None:
        assessment = await self._owned_assessment(telegram_user_id, assessment_id)
        if assessment.status is not AssessmentStatus.COMPLETED:
            raise StaleInput("Only completed results can be deleted individually")
        await self._repository.delete_assessment(assessment_id, telegram_user_id)

    async def delete_user(self, telegram_user_id: int) -> None:
        await self._repository.delete_user(telegram_user_id)

    async def _reject_expired(self, assessment: Assessment, now: datetime) -> None:
        if (
            assessment.status is AssessmentStatus.ACTIVE
            and now - assessment.updated_at >= timedelta(days=30)
        ):
            assessment.status = AssessmentStatus.EXPIRED
            assessment.updated_at = now
            await self._repository.save_assessment(assessment)
            raise StaleInput("This assessment has expired")

    async def _owned_assessment(self, telegram_user_id: int, assessment_id: str) -> Assessment:
        assessment = await self._repository.get_assessment(assessment_id)
        if assessment is None or assessment.telegram_user_id != telegram_user_id:
            raise AssessmentNotFound("Assessment not found")
        return assessment

    def _questionnaire_for(self, assessment: Assessment) -> Questionnaire:
        questionnaire = self._questionnaires.get(assessment.questionnaire_id)
        if questionnaire is None or questionnaire.version != assessment.questionnaire_version:
            raise AssessmentNotFound("Questionnaire version is unavailable")
        return questionnaire

    def _view(self, assessment: Assessment) -> AssessmentView:
        questionnaire = self._questionnaire_for(assessment)
        return AssessmentView.from_assessment(
            assessment,
            tuple(item.id for item in questionnaire.items),
        )
