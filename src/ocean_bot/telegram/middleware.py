from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import monotonic
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class UserActionLocks:
    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}

    @asynccontextmanager
    async def hold(self, user_id: int) -> AsyncIterator[None]:
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            yield


class RateLimitMiddleware(BaseMiddleware):
    def __init__(
        self,
        limit: int = 30,
        window_seconds: float = 60,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("Rate limit and window must be positive")
        self._limit = limit
        self._window_seconds = window_seconds
        self._clock = clock
        self._events: defaultdict[int, deque[float]] = defaultdict(deque)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)
        now = self._clock()
        timestamps = self._events[user.id]
        cutoff = now - self._window_seconds
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        if len(timestamps) >= self._limit:
            if isinstance(event, CallbackQuery):
                await event.answer("Too many requests. Please wait a moment.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Too many requests. Please wait a moment.")
            return None
        timestamps.append(now)
        return await handler(event, data)
