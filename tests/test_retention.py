import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ocean_bot.application.retention import DataRetention, RetentionHealth, RetentionResult


async def test_retention_applies_policy_and_reports_completed_work() -> None:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    assessments = SimpleNamespace(expire_incomplete_before=AsyncMock(return_value=2))
    analytics = SimpleNamespace(purge_before=AsyncMock(return_value=7))
    retention = DataRetention(assessments, analytics)  # type: ignore[arg-type]

    result = await retention.run(now)

    assert result == RetentionResult(expired_assessments=2, purged_analytics_events=7)
    assessments.expire_incomplete_before.assert_awaited_once_with(
        now - timedelta(days=30), now
    )
    analytics.purge_before.assert_awaited_once_with(now - timedelta(days=90))


async def test_retention_retries_and_recovers_after_transient_error() -> None:
    assessments = SimpleNamespace(
        expire_incomplete_before=AsyncMock(side_effect=[RuntimeError("down"), 0])
    )
    analytics = SimpleNamespace(purge_before=AsyncMock(return_value=0))
    retention = DataRetention(assessments, analytics)  # type: ignore[arg-type]
    health = RetentionHealth()
    sleeps = 0

    async def stop_after_retry(_: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await retention.run_forever(health, sleep=stop_after_retry)

    assert assessments.expire_incomplete_before.await_count == 2
    assert health.healthy is True
