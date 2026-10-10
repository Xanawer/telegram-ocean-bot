from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import monotonic
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from ocean_bot.telegram.delivery import TelegramDelivery

_callback_delivery = TelegramDelivery(max_per_second=50)
_message_delivery = TelegramDelivery(max_per_second=25)


class UserActionLocks:
    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}

    @asynccontextmanager
    async def hold(self, user_id: int) -> AsyncGenerator[None, None]:
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            yield


class UserActionLockMiddleware(BaseMiddleware):
    """Serialize a user's commands with their callbacks.

    Commands such as /test can import remote state for the user; holding the same lock as
    the deletion callback keeps that import from landing after the user's deletion.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        locks = data.get("action_locks")
        user = getattr(event, "from_user", None)
        if not isinstance(locks, UserActionLocks) or user is None:
            return await handler(event, data)
        async with locks.hold(user.id):
            return await handler(event, data)


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
                await _callback_delivery.call(
                    lambda: event.answer(
                        "Too many requests. Please wait a moment.", show_alert=True
                    ),
                    "callback.answer.rate_limit",
                )
            elif isinstance(event, Message):
                await _message_delivery.call(
                    lambda: event.answer("Too many requests. Please wait a moment."),
                    "message.answer.rate_limit",
                )
            return None
        timestamps.append(now)
        return await handler(event, data)
