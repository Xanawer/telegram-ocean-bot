from __future__ import annotations

import logging
from collections.abc import Mapping

from sqlalchemy.exc import SQLAlchemyError

_DRIVER_MODULES = frozenset({"asyncpg", "sqlite3", "aiosqlite"})


def _database_errors(error: BaseException) -> list[str]:
    names: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if (
            isinstance(current, SQLAlchemyError)
            or type(current).__module__.split(".")[0] in _DRIVER_MODULES
        ):
            names.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return names


def _redact(value: object) -> object:
    if isinstance(value, BaseException) and (names := _database_errors(value)):
        return f"<redacted {'>'.join(names)}>"
    return value


class DatabaseErrorRedaction(logging.Filter):
    """Keep database error text out of logs.

    Driver messages can quote the failing row, such as a Telegram ID or stored answers, even
    when SQLAlchemy hides bound parameters. Records keep their message and error type names.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and record.exc_info[1] is not None:
            names = _database_errors(record.exc_info[1])
            if names:
                record.exc_info = None
                record.exc_text = None
                record.msg = f"{record.msg} database_error={'>'.join(names)}"
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(arg) for arg in record.args)
        elif isinstance(record.args, Mapping):
            record.args = {key: _redact(value) for key, value in record.args.items()}
        record.msg = _redact(record.msg)
        return True


def install_log_redaction() -> None:
    redaction = DatabaseErrorRedaction()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redaction)
