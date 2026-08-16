from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ocean_bot.application.analytics import AnalyticsRecord
from ocean_bot.domain.assessment import Assessment, UserRecord


class DataRightsUnavailable(Exception):
    pass


class UserReferences:
    def __init__(self, pepper: str) -> None:
        if len(pepper) < 8:
            raise ValueError("Analytics pepper must contain at least 8 characters")
        self._pepper = pepper.encode()

    def __call__(self, telegram_user_id: int) -> str:
        return hmac.new(
            self._pepper,
            str(telegram_user_id).encode(),
            hashlib.sha256,
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredUserData:
    user: UserRecord | None
    assessments: tuple[Assessment, ...]
    analytics: tuple[AnalyticsRecord, ...]


@dataclass(frozen=True, slots=True)
class RevisionedUserData:
    data: StoredUserData
    assessment_revisions: Mapping[str, int]


class UserDataRightsRepository(Protocol):
    async def load_user_data(self, telegram_user_id: int, user_ref: str) -> StoredUserData: ...

    async def delete_user_data(self, telegram_user_id: int, user_ref: str) -> None: ...


class RevisionedUserDataRightsRepository(UserDataRightsRepository, Protocol):
    async def load_user_data_with_revisions(
        self, telegram_user_id: int, user_ref: str
    ) -> RevisionedUserData: ...


class LocalUserDataRightsRepository(RevisionedUserDataRightsRepository, Protocol):
    async def mark_deletion(self, telegram_user_id: int, user_ref: str, now: datetime) -> None: ...

    async def finalize_deletion(self, telegram_user_id: int) -> None: ...

    async def deletion_user_ref(self, telegram_user_id: int) -> str | None: ...

    async def purge_user_data(self, telegram_user_id: int, user_ref: str) -> None: ...


@dataclass(frozen=True, slots=True)
class UserDataExport:
    document: dict[str, object]

    def json_bytes(self) -> bytes:
        return json.dumps(self.document, indent=2).encode()


class UserDataRights:
    def __init__(
        self,
        repository: UserDataRightsRepository,
        user_references: UserReferences,
    ) -> None:
        self._repository = repository
        self._user_references = user_references

    async def export_user(self, telegram_user_id: int) -> UserDataExport:
        stored = await self._repository.load_user_data(
            telegram_user_id,
            self._user_references(telegram_user_id),
        )
        user_data: dict[str, object] | None = None
        if stored.user is not None:
            user_data = {
                "telegram_user_id": stored.user.telegram_user_id,
                "language_code": stored.user.language_code,
                "accepted_at": stored.user.accepted_at.isoformat(),
            }
        assessments = [
            {
                "id": assessment.id,
                "questionnaire_id": assessment.questionnaire_id,
                "questionnaire_version": assessment.questionnaire_version,
                "status": assessment.status.value,
                "answers": dict(assessment.answers),
                "scores": {trait.value: score for trait, score in assessment.scores},
                "facet_scores": dict(assessment.facet_scores),
                "started_at": assessment.started_at.isoformat(),
                "completed_at": (
                    assessment.completed_at.isoformat()
                    if assessment.completed_at is not None
                    else None
                ),
            }
            for assessment in stored.assessments
        ]
        analytics = [
            {
                "name": record.name,
                "occurred_at": record.occurred_at.isoformat(),
                "properties": dict(record.properties),
            }
            for record in stored.analytics
        ]
        return UserDataExport(
            {"user": user_data, "assessments": assessments, "analytics": analytics}
        )

    async def delete_user(self, telegram_user_id: int) -> None:
        await self._repository.delete_user_data(
            telegram_user_id,
            self._user_references(telegram_user_id),
        )
