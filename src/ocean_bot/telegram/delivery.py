from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from aiogram.exceptions import TelegramRetryAfter

_logger = logging.getLogger(__name__)
T = TypeVar("T")


class TelegramDelivery:
    """Bound and retry Telegram API calls that deliver user-facing responses."""

    def __init__(
        self,
        *,
        max_in_flight: int = 20,
        max_retries: int = 3,
        max_per_second: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_in_flight < 1:
            raise ValueError("max_in_flight must be positive")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if max_per_second is not None and max_per_second <= 0:
            raise ValueError("max_per_second must be positive when configured")
        self._semaphore = asyncio.Semaphore(max_in_flight)
        self._max_retries = max_retries
        self._rate_interval = 1 / max_per_second if max_per_second is not None else None
        self._rate_lock = asyncio.Lock()
        self._next_rate_slot = 0.0
        self._sleep = sleep

    async def call(
        self,
        operation: Callable[[], Awaitable[T]],
        operation_name: str,
    ) -> T:
        for attempt in range(self._max_retries + 1):
            try:
                async with self._semaphore:
                    await self._wait_for_rate_slot()
                    return await operation()
            except TelegramRetryAfter as error:
                if attempt >= self._max_retries:
                    raise
                delay = max(0.0, float(error.retry_after))
                _logger.warning(
                    "telegram_retry_after operation=%s retry_after_s=%.1f attempt=%d",
                    operation_name,
                    delay,
                    attempt + 1,
                )
                await self._sleep(delay)
        raise AssertionError("Telegram delivery retry loop exhausted unexpectedly")

    async def _wait_for_rate_slot(self) -> None:
        if self._rate_interval is None:
            return
        loop = asyncio.get_running_loop()
        async with self._rate_lock:
            now = loop.time()
            slot = max(now, self._next_rate_slot)
            self._next_rate_slot = slot + self._rate_interval
        if slot > now:
            await asyncio.sleep(slot - now)
