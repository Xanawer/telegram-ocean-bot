from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AnalyticsRecord:
    user_ref: str
    name: str
    occurred_at: datetime
    properties: Mapping[str, str]


class AnalyticsRepository(Protocol):
    async def add_event(self, event: AnalyticsRecord) -> None: ...
    async def list_for_user(self, user_ref: str) -> list[AnalyticsRecord]: ...
    async def counts(self) -> dict[str, int]: ...
    async def delete_before(self, cutoff: datetime) -> int: ...
    async def delete_for_user(self, user_ref: str) -> None: ...


class AnalyticsService:
    def __init__(self, repository: AnalyticsRepository, pepper: str) -> None:
        if len(pepper) < 8:
            raise ValueError("Analytics pepper must contain at least 8 characters")
        self._repository = repository
        self._pepper = pepper.encode()

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
                user_ref=self.user_ref(telegram_user_id),
                name=name,
                occurred_at=occurred_at,
                properties=dict(properties or {}),
            )
        )

    async def stats(self) -> dict[str, int]:
        return await self._repository.counts()

    async def export_user(self, telegram_user_id: int) -> list[dict[str, object]]:
        records = await self._repository.list_for_user(self.user_ref(telegram_user_id))
        return [
            {
                "name": record.name,
                "occurred_at": record.occurred_at.isoformat(),
                "properties": dict(record.properties),
            }
            for record in records
        ]

    async def purge(self, now: datetime) -> int:
        return await self._repository.delete_before(now - timedelta(days=90))

    async def delete_user(self, telegram_user_id: int) -> None:
        await self._repository.delete_for_user(self.user_ref(telegram_user_id))

    def user_ref(self, telegram_user_id: int) -> str:
        return hmac.new(
            self._pepper,
            str(telegram_user_id).encode(),
            hashlib.sha256,
        ).hexdigest()
