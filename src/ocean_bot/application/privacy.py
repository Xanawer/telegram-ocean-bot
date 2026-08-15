from __future__ import annotations

from typing import Protocol

from ocean_bot.application.analytics import AnalyticsService


class PrivacyRepository(Protocol):
    async def delete_user_data(self, telegram_user_id: int, user_ref: str) -> None: ...


class PrivacyService:
    def __init__(self, repository: PrivacyRepository, analytics: AnalyticsService) -> None:
        self._repository = repository
        self._analytics = analytics

    async def delete_user(self, telegram_user_id: int) -> None:
        await self._repository.delete_user_data(
            telegram_user_id,
            self._analytics.user_ref(telegram_user_id),
        )
