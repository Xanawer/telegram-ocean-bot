import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.ext.asyncio import create_async_engine

from ocean_bot.config import Settings
from ocean_bot.infrastructure.database import Base
from ocean_bot.main import (
    RetentionResult,
    RetentionState,
    _run_retention,
    _webhook_app,
    configure_webhook,
    database_is_ready,
    run_retention_once,
)


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


async def test_one_shot_retention_reports_completed_work() -> None:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    service = SimpleNamespace(expire_incomplete=AsyncMock(return_value=2))
    analytics = SimpleNamespace(purge=AsyncMock(return_value=7))

    result = await run_retention_once(service, analytics, now)

    assert result == RetentionResult(expired_assessments=2, purged_analytics_events=7)
    service.expire_incomplete.assert_awaited_once_with(now)
    analytics.purge.assert_awaited_once_with(now)


async def test_database_readiness_executes_a_real_query() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        assert await database_is_ready(engine) is True
    finally:
        await engine.dispose()


async def test_configure_webhook_registers_authenticated_endpoint() -> None:
    settings = Settings(
        bot_token="123456:test-token",
        database_url="sqlite+aiosqlite:///local.db",
        analytics_pepper="long-test-pepper",
        admin_user_id=42,
        privacy_contact="privacy@example.com",
        webhook_base_url="https://bot.example.com/",
        webhook_secret="s" * 32,
    )
    bot = SimpleNamespace(set_my_commands=AsyncMock(), set_webhook=AsyncMock())

    await configure_webhook(bot, settings)

    bot.set_my_commands.assert_awaited_once()
    bot.set_webhook.assert_awaited_once_with(
        "https://bot.example.com/telegram/webhook",
        secret_token="s" * 32,
    )


def test_runtime_cli_advertises_service_and_job_commands() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "ocean_bot", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "{serve,retention,set-webhook}" in completed.stdout


async def test_database_readiness_reports_unavailable_without_raising() -> None:
    class UnavailableEngine:
        def connect(self) -> None:
            raise ConnectionError("database unavailable")

    assert await database_is_ready(UnavailableEngine()) is False  # type: ignore[arg-type]


def test_retention_cli_runs_one_cycle_and_exits(tmp_path: Path) -> None:
    database_path = tmp_path / "retention.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"

    async def create_schema() -> None:
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create_schema())
    environment = os.environ.copy()
    environment.update(
        {
            "OCEAN_BOT_TOKEN": "123456:test-token",
            "OCEAN_DATABASE_URL": database_url,
            "OCEAN_ANALYTICS_PEPPER": "long-test-pepper",
            "OCEAN_ADMIN_USER_ID": "42",
            "OCEAN_PRIVACY_CONTACT": "privacy@example.com",
            "OCEAN_RETENTION_MODE": "external",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-m", "ocean_bot", "retention"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert json.loads(completed.stdout) == {
        "expired_assessments": 0,
        "purged_analytics_events": 0,
    }


async def test_webhook_health_checks_database_without_startup_side_effects() -> None:
    settings = Settings(
        bot_token="123456:test-token",
        database_url="sqlite+aiosqlite://",
        analytics_pepper="long-test-pepper",
        admin_user_id=42,
        privacy_contact="privacy@example.com",
        webhook_base_url="https://bot.example.com",
        webhook_secret="s" * 32,
        retention_mode="external",
        webhook_setup_mode="external",
    )

    async with TestClient(TestServer(_webhook_app(settings))) as client:
        live = await client.get("/live")
        live_body = await live.json()
        ready = await client.get("/health")
        ready_body = await ready.json()

    assert live.status == 200
    assert live_body == {"status": "ok"}
    assert ready.status == 200
    assert ready_body == {"status": "ok"}
