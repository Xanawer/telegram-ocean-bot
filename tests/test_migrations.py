import logging
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

from ocean_bot.infrastructure.local import create_local_engine
from ocean_bot.infrastructure.migrations import upgrade_database

PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def test_local_volume_has_the_replication_compatible_schema(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'migrated.db'}"
    await upgrade_database(database_url)
    engine = create_local_engine(database_url)
    async with engine.connect() as connection:
        tables = {
            row[0]
            for row in (
                await connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ).all()
        }

    assert {"assessments", "analytics_events", "sync_outbox"}.issubset(tables)
    await engine.dispose()


async def test_replication_migration_backfills_existing_analytics_ids(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"
    await upgrade_database(database_url, "5a1b2c3d4e5f")
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            "INSERT INTO analytics_events "
            "(user_ref, name, occurred_at, properties) "
            "VALUES ('ref', 'answer_recorded', '2026-01-01', '{}')"
        )
    await engine.dispose()

    await upgrade_database(database_url)

    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        event_id = (
            await connection.exec_driver_sql("SELECT event_id FROM analytics_events")
        ).scalar_one()
    assert event_id == "legacy-1"
    await engine.dispose()


def test_database_url_with_percent_encoded_characters_is_accepted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = "postgresql://user:p%40ss@localhost/ocean"

    command.upgrade(config, "head", sql=True)

    assert "CREATE TABLE" in capsys.readouterr().out


async def test_in_process_upgrade_keeps_existing_loggers_enabled(tmp_path: Path) -> None:
    logger = logging.getLogger("ocean_bot.test")
    aiogram_logger = logging.getLogger("aiogram.test")

    await upgrade_database(f"sqlite+aiosqlite:///{tmp_path / 'logging.db'}")

    assert not logger.disabled
    assert not aiogram_logger.disabled
