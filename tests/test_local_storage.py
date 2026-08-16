from pathlib import Path

from ocean_bot.infrastructure.local import create_local_engine


async def test_local_engine_applies_the_durability_contract(tmp_path: Path) -> None:
    engine = create_local_engine(f"sqlite+aiosqlite:///{tmp_path / 'durable.db'}")
    async with engine.connect() as connection:
        journal_mode = (await connection.exec_driver_sql("PRAGMA journal_mode")).scalar_one()
        synchronous = (await connection.exec_driver_sql("PRAGMA synchronous")).scalar_one()

    assert journal_mode.lower() == "wal"
    assert synchronous == 2
    await engine.dispose()
