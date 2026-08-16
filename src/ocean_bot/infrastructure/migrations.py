from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config


async def upgrade_database(database_url: str, revision: str = "head") -> None:
    await asyncio.to_thread(_upgrade_database_sync, database_url, revision)


def _upgrade_database_sync(database_url: str, revision: str) -> None:
    project_root = Path(__file__).resolve().parents[3]
    config = Config(str(project_root / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, revision)
