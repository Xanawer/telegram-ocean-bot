import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ocean_bot.main import RetentionState, _run_retention


async def test_retention_retries_and_recovers_after_transient_error() -> None:
    service = SimpleNamespace(expire_incomplete=AsyncMock(side_effect=[RuntimeError("down"), 0]))
    analytics = SimpleNamespace(purge=AsyncMock(return_value=0))
    state = RetentionState()
    sleeps = 0

    async def stop_after_retry(_: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _run_retention(service, analytics, state, sleep=stop_after_retry)

    assert service.expire_incomplete.await_count == 2
    assert state.healthy is True
