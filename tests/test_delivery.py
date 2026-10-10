import asyncio

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from ocean_bot.telegram.delivery import TelegramDelivery


async def test_telegram_delivery_retries_with_server_delay() -> None:
    attempts = 0
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TelegramRetryAfter(
                method=SendMessage(chat_id=1, text="test"),
                message="retry",
                retry_after=2,
            )
        return "sent"

    delivery = TelegramDelivery(max_in_flight=1, max_per_second=None, sleep=sleep)

    assert await delivery.call(operation, "test.send") == "sent"
    assert attempts == 3
    assert delays == [2, 2]


async def test_telegram_delivery_bounds_in_flight_operations() -> None:
    active = 0
    peak = 0

    async def operation() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1

    delivery = TelegramDelivery(max_in_flight=2, max_per_second=None)

    await asyncio.gather(*(delivery.call(operation, "test.send") for _ in range(20)))

    assert peak <= 2


async def test_telegram_delivery_raises_after_retry_budget() -> None:
    attempts = 0

    async def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise TelegramRetryAfter(
            method=SendMessage(chat_id=1, text="test"),
            message="retry",
            retry_after=0,
        )

    delivery = TelegramDelivery(
        max_retries=2,
        max_per_second=None,
        sleep=lambda _: asyncio.sleep(0),
    )

    with pytest.raises(TelegramRetryAfter):
        await delivery.call(operation, "test.send")
    assert attempts == 3


async def test_telegram_delivery_gives_up_on_long_flood_control_waits() -> None:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async def operation() -> str:
        raise TelegramRetryAfter(
            method=SendMessage(chat_id=1, text="test"),
            message="retry",
            retry_after=600,
        )

    delivery = TelegramDelivery(max_per_second=None, max_retry_after=10, sleep=sleep)

    with pytest.raises(TelegramRetryAfter):
        await delivery.call(operation, "test.send")
    assert delays == []
