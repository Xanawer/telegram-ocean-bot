from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4

from ocean_bot.application.analytics import AnalyticsEvent
from ocean_bot.domain.assessment import (
    Assessment,
    AssessmentStateError,
    AssessmentStatus,
    AssessmentView,
    UserRecord,
)
from ocean_bot.domain.questionnaire import Questionnaire


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


class StorageUnavailable(AssessmentError):
    pass


class SyncBacklogExceeded(AssessmentError):
    pass


@dataclass(frozen=True, slots=True)
class StoredAssessment:
    assessment: Assessment
    token: object


@dataclass(frozen=True, slots=True)
class AssessmentTransition:
    view: AssessmentView
    changed: bool


type AssessmentEventFactory = Callable[[Assessment], AnalyticsEvent]


class AssessmentRepository(Protocol):
    async def save_user(self, user: UserRecord) -> None: ...
    async def get_user(self, telegram_user_id: int) -> UserRecord | None: ...
    async def create_assessment(self, assessment: Assessment) -> StoredAssessment: ...
    async def save_assessment(
        self,
        stored: StoredAssessment,
        assessment: Assessment,
        event: AnalyticsEvent | None = None,
    ) -> StoredAssessment: ...
    async def get_assessment(self, assessment_id: str) -> StoredAssessment | None: ...
    async def get_active(self, telegram_user_id: int) -> StoredAssessment | None: ...
    async def list_completed(self, telegram_user_id: int) -> list[StoredAssessment]: ...
    async def list_active_before(self, cutoff: datetime) -> list[StoredAssessment]: ...
    async def delete_assessment(self, assessment_id: str, telegram_user_id: int) -> None: ...


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
        questionnaire = self._questionnaires.get(questionnaire_id)
        if questionnaire is None:
            raise AssessmentNotFound("Unknown questionnaire")
        active = await self._repository.get_active(telegram_user_id)
        if active is not None:
            raise ActiveAssessmentExists(active.assessment.id)
        assessment = Assessment.start(str(uuid4()), telegram_user_id, questionnaire, now)
        stored = await self._repository.create_assessment(assessment)
        return self._view(stored.assessment)

    async def answer(
        self,
        telegram_user_id: int,
        assessment_id: str,
        item_id: str,
        value: int,
        now: datetime,
        event_factory: AssessmentEventFactory | None = None,
    ) -> AssessmentTransition:
        stored = await self._owned_assessment(telegram_user_id, assessment_id)
        await self._reject_expired(stored, now)
        questionnaire = self._questionnaire_for(stored.assessment)
        try:
            updated = stored.assessment.answer(questionnaire, item_id, value, now)
        except AssessmentStateError as error:
            raise StaleInput(str(error)) from error
        if updated is stored.assessment:
            return AssessmentTransition(self._view(updated), changed=False)
        transition_event = event_factory(updated) if event_factory is not None else None
        saved = await self._repository.save_assessment(
            stored,
            updated,
            transition_event,
        )
        return AssessmentTransition(self._view(saved.assessment), changed=True)

    async def history(self, telegram_user_id: int) -> list[AssessmentView]:
        assessments = await self._repository.list_completed(telegram_user_id)
        return [self._view(stored.assessment) for stored in assessments]

    async def get(self, telegram_user_id: int, assessment_id: str) -> AssessmentView:
        stored = await self._owned_assessment(telegram_user_id, assessment_id)
        return self._view(stored.assessment)

    async def resume(self, telegram_user_id: int, now: datetime) -> AssessmentView | None:
        stored = await self._repository.get_active(telegram_user_id)
        if stored is None:
            return None
        assessment = stored.assessment
        if self._is_expired(assessment, now):
            await self._repository.save_assessment(stored, assessment.expire(now))
            return None
        return self._view(assessment)

    async def back(
        self,
        telegram_user_id: int,
        assessment_id: str,
        now: datetime,
        event_factory: AssessmentEventFactory | None = None,
        expected_answered: int | None = None,
    ) -> AssessmentTransition:
        stored = await self._owned_assessment(telegram_user_id, assessment_id)
        await self._reject_expired(stored, now)
        if expected_answered is not None and len(stored.assessment.answers) != expected_answered:
            raise StaleInput("This question has already changed")
        try:
            updated = stored.assessment.back(self._questionnaire_for(stored.assessment), now)
        except AssessmentStateError as error:
            raise StaleInput(str(error)) from error
        if updated is stored.assessment:
            return AssessmentTransition(self._view(updated), changed=False)
        event = event_factory(updated) if event_factory is not None else None
        saved = await self._repository.save_assessment(stored, updated, event)
        return AssessmentTransition(self._view(saved.assessment), changed=True)

    async def cancel(
        self,
        telegram_user_id: int,
        assessment_id: str,
        now: datetime,
        event_factory: AssessmentEventFactory | None = None,
    ) -> AssessmentTransition:
        stored = await self._owned_assessment(telegram_user_id, assessment_id)
        await self._reject_expired(stored, now)
        try:
            updated = stored.assessment.cancel(now)
        except AssessmentStateError as error:
            raise StaleInput(str(error)) from error
        event = event_factory(updated) if event_factory is not None else None
        saved = await self._repository.save_assessment(stored, updated, event)
        return AssessmentTransition(self._view(saved.assessment), changed=True)

    async def expire_incomplete_before(self, cutoff: datetime, now: datetime) -> int:
        stored_assessments = await self._repository.list_active_before(cutoff)
        for stored in stored_assessments:
            await self._repository.save_assessment(stored, stored.assessment.expire(now))
        return len(stored_assessments)

    async def delete_assessment(self, telegram_user_id: int, assessment_id: str) -> None:
        stored = await self._owned_assessment(telegram_user_id, assessment_id)
        if stored.assessment.status is not AssessmentStatus.COMPLETED:
            raise StaleInput("Only completed results can be deleted individually")
        await self._repository.delete_assessment(assessment_id, telegram_user_id)

    async def _reject_expired(self, stored: StoredAssessment, now: datetime) -> None:
        assessment = stored.assessment
        if self._is_expired(assessment, now):
            await self._repository.save_assessment(stored, assessment.expire(now))
            raise StaleInput("This assessment has expired")

    async def _owned_assessment(
        self, telegram_user_id: int, assessment_id: str
    ) -> StoredAssessment:
        stored = await self._repository.get_assessment(assessment_id)
        if stored is None or stored.assessment.telegram_user_id != telegram_user_id:
            raise AssessmentNotFound("Assessment not found")
        return stored

    def _questionnaire_for(self, assessment: Assessment) -> Questionnaire:
        questionnaire = self._questionnaires.get(assessment.questionnaire_id)
        if questionnaire is None or questionnaire.version != assessment.questionnaire_version:
            raise AssessmentNotFound("Questionnaire version is unavailable")
        return questionnaire

    def _view(self, assessment: Assessment) -> AssessmentView:
        try:
            return assessment.view(self._questionnaire_for(assessment))
        except AssessmentStateError as error:
            raise AssessmentNotFound(str(error)) from error

    @staticmethod
    def _is_expired(assessment: Assessment, now: datetime) -> bool:
        return (
            assessment.status is AssessmentStatus.ACTIVE
            and now - assessment.updated_at >= timedelta(days=30)
        )
