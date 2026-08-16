import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.ext.asyncio import create_async_engine

from ocean_bot.application.analytics import AnalyticsRecord
from ocean_bot.config import Settings
from ocean_bot.infrastructure.database import (
    Base,
    SqlAnalyticsRepository,
    session_factory,
)
from ocean_bot.infrastructure.local import create_local_engine
from ocean_bot.infrastructure.migrations import upgrade_database
from ocean_bot.runtime import Runtime


def _settings(database_url: str, **values: object) -> Settings:
    return Settings(
        bot_token="123456:test-token",
        database_url=database_url,
        analytics_pepper="long-test-pepper",
        admin_user_id=42,
        privacy_contact="privacy@example.com",
        **values,
    )


async def _create_schema(database_url: str) -> None:
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()


def test_runtime_cli_advertises_service_and_job_commands() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "ocean_bot", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "{serve,retention,set-webhook,sync}" in completed.stdout


def test_retention_cli_runs_one_cycle_without_constructing_telegram(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'retention.db'}"
    asyncio.run(_create_schema(database_url))
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


async def test_retention_runtime_constructs_only_database(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'runtime-retention.db'}"
    await _create_schema(database_url)

    def fail_bot(_: str) -> None:
        raise AssertionError("retention must not construct Telegram")

    runtime = Runtime(
        _settings(database_url),
        bot_factory=fail_bot,  # type: ignore[arg-type]
    )

    result = await runtime.run_retention()

    assert result.expired_assessments == 0
    assert result.purged_analytics_events == 0


async def test_authoritative_retention_purges_local_and_remote_analytics(
    tmp_path: Path,
) -> None:
    remote_url = f"sqlite+aiosqlite:///{tmp_path / 'remote-retention.db'}"
    local_url = f"sqlite+aiosqlite:///{tmp_path / 'local-retention.db'}"
    await _create_schema(remote_url)
    await upgrade_database(local_url)
    remote_engine = create_async_engine(remote_url)
    local_engine = create_local_engine(local_url)
    remote = SqlAnalyticsRepository(session_factory(remote_engine))
    local = SqlAnalyticsRepository(session_factory(local_engine))
    old = datetime(2025, 1, 1, tzinfo=UTC)
    await remote.add_event(AnalyticsRecord("remote-old", "ref", "event", old, {}))
    await local.add_event(AnalyticsRecord("local-old", "ref", "event", old, {}))
    await remote_engine.dispose()
    await local_engine.dispose()

    result = await Runtime(
        _settings(
            remote_url,
            storage_mode="authoritative",
            local_database_url=local_url,
        )
    ).run_retention()

    remote_engine = create_async_engine(remote_url)
    local_engine = create_local_engine(local_url)
    remote = SqlAnalyticsRepository(session_factory(remote_engine))
    local = SqlAnalyticsRepository(session_factory(local_engine))
    assert result.purged_analytics_events == 2
    assert await remote.delete_before(datetime(2027, 1, 1, tzinfo=UTC)) == 0
    assert await local.delete_before(datetime(2027, 1, 1, tzinfo=UTC)) == 0
    await remote_engine.dispose()
    await local_engine.dispose()


async def test_sync_runtime_migrates_local_volume_and_drains_queue(
    tmp_path: Path,
) -> None:
    remote_url = f"sqlite+aiosqlite:///{tmp_path / 'remote.db'}"
    local_url = f"sqlite+aiosqlite:///{tmp_path / 'local.db'}"
    await _create_schema(remote_url)
    runtime = Runtime(
        _settings(
            remote_url,
            storage_mode="authoritative",
            local_database_url=local_url,
        )
    )

    result = await runtime.run_sync()

    assert result.remaining == 0
    assert (tmp_path / "local.db").exists()


async def test_set_webhook_constructs_only_telegram_dependency() -> None:
    settings = _settings(
        "sqlite+aiosqlite:///unused.db",
        webhook_base_url="https://bot.example.com/",
        webhook_secret="s" * 32,
    )
    fake_bot = SimpleNamespace(
        set_my_commands=AsyncMock(),
        set_webhook=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )

    def fail_engine(_: str) -> None:
        raise AssertionError("set-webhook must not construct a database engine")

    runtime = Runtime(
        settings,
        bot_factory=lambda _: fake_bot,  # type: ignore[arg-type,return-value]
        engine_factory=fail_engine,  # type: ignore[arg-type]
    )

    await runtime.set_webhook()

    fake_bot.set_my_commands.assert_awaited_once()
    fake_bot.set_webhook.assert_awaited_once_with(
        "https://bot.example.com/telegram/webhook",
        secret_token="s" * 32,
    )
    fake_bot.session.close.assert_awaited_once()


async def test_database_readiness_executes_a_real_query() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        assert await Runtime(_settings("sqlite+aiosqlite://")).database_is_ready(engine)
    finally:
        await engine.dispose()


async def test_database_readiness_reports_unavailable_without_raising() -> None:
    class UnavailableEngine:
        def connect(self) -> None:
            raise ConnectionError("database unavailable")

    runtime = Runtime(_settings("sqlite+aiosqlite://"))
    assert await runtime.database_is_ready(UnavailableEngine()) is False  # type: ignore[arg-type]


async def test_webhook_health_checks_database_without_startup_side_effects() -> None:
    settings = _settings(
        "sqlite+aiosqlite://",
        webhook_base_url="https://bot.example.com",
        webhook_secret="s" * 32,
        retention_mode="external",
        webhook_setup_mode="external",
    )

    async with TestClient(TestServer(Runtime(settings).webhook_app())) as client:
        live = await client.get("/live")
        live_body = await live.json()
        ready = await client.get("/health")
        ready_body = await ready.json()

    assert live.status == 200
    assert live_body == {"status": "ok"}
    assert ready.status == 200
    assert ready_body == {"status": "ok"}
