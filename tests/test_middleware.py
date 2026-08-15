from types import SimpleNamespace
from unittest.mock import AsyncMock

from ocean_bot.telegram.middleware import RateLimitMiddleware


async def test_rate_limit_stops_excess_events_per_user() -> None:
    middleware = RateLimitMiddleware(limit=2, window_seconds=60, clock=lambda: 100.0)
    handler = AsyncMock(return_value="handled")
    event = SimpleNamespace(from_user=SimpleNamespace(id=42))

    assert await middleware(handler, event, {}) == "handled"
    assert await middleware(handler, event, {}) == "handled"
    assert await middleware(handler, event, {}) is None
    assert handler.await_count == 2
