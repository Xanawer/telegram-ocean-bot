from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class AnalyticsRecord:
    event_id: str
    user_ref: str
    name: str
    occurred_at: datetime
    properties: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class AnalyticsEvent:
    name: str
    occurred_at: datetime
    properties: Mapping[str, str]


class AnalyticsRepository(Protocol):
    async def add_event(self, event: AnalyticsRecord) -> None: ...
    async def counts(self) -> dict[str, int]: ...
    async def delete_before(self, cutoff: datetime) -> int: ...


class AnalyticsService:
    def __init__(
        self,
        repository: AnalyticsRepository,
        user_reference: Callable[[int], str],
    ) -> None:
        self._repository = repository
        self._user_reference = user_reference

    async def record(
        self,
        telegram_user_id: int,
        name: str,
        occurred_at: datetime,
        properties: Mapping[str, str] | None = None,
    ) -> None:
        if not name or len(name) > 64:
            raise ValueError("Analytics event names must contain 1-64 characters")
        await self._repository.add_event(
            AnalyticsRecord(
                event_id=str(uuid4()),
                user_ref=self._user_reference(telegram_user_id),
                name=name,
                occurred_at=occurred_at,
                properties=dict(properties or {}),
            )
        )

    async def stats(self) -> dict[str, int]:
        return await self._repository.counts()

    async def purge_before(self, cutoff: datetime) -> int:
        return await self._repository.delete_before(cutoff)
