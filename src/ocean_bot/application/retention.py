from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ocean_bot.application.analytics import AnalyticsService
from ocean_bot.application.assessments import AssessmentService

_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RetentionHealth:
    healthy: bool = True


@dataclass(frozen=True, slots=True)
class RetentionResult:
    expired_assessments: int
    purged_analytics_events: int


class DataRetention:
    ASSESSMENT_RETENTION = timedelta(days=30)
    ANALYTICS_RETENTION = timedelta(days=90)
    RUN_INTERVAL_SECONDS = 24 * 60 * 60
    RETRY_SECONDS = 60

    def __init__(
        self,
        assessments: AssessmentService,
        analytics: AnalyticsService,
    ) -> None:
        self._assessments = assessments
        self._analytics = analytics

    async def run(self, now: datetime | None = None) -> RetentionResult:
        effective_now = now or datetime.now(UTC)
        expired = await self._assessments.expire_incomplete_before(
            effective_now - self.ASSESSMENT_RETENTION,
            effective_now,
        )
        purged = await self._analytics.purge_before(
            effective_now - self.ANALYTICS_RETENTION
        )
        return RetentionResult(expired, purged)

    async def run_forever(
        self,
        health: RetentionHealth,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        while True:
            try:
                await self.run()
            except asyncio.CancelledError:
                raise
            except Exception:
                health.healthy = False
                _logger.exception("Retention cycle failed; retrying")
                await sleep(self.RETRY_SECONDS)
                continue
            health.healthy = True
            await sleep(self.RUN_INTERVAL_SECONDS)
