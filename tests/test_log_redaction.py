import io
import logging
import sqlite3

from sqlalchemy.exc import IntegrityError

from ocean_bot.application.assessments import StorageUnavailable
from ocean_bot.log_redaction import DatabaseErrorRedaction


def _logger() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(DatabaseErrorRedaction())
    logger = logging.getLogger("ocean_bot.test_redaction")
    logger.handlers = [handler]
    logger.propagate = False
    return logger, stream


def _database_error() -> IntegrityError:
    driver = sqlite3.IntegrityError("Key (telegram_user_id)=(424242) already exists")
    return IntegrityError("INSERT INTO users", {"telegram_user_id": 424242}, driver)


def test_traceback_of_chained_database_error_is_dropped() -> None:
    logger, stream = _logger()
    try:
        try:
            raise _database_error()
        except IntegrityError as error:
            raise StorageUnavailable("Your answer could not be saved") from error
    except StorageUnavailable:
        logger.exception("Shadow assessment mirror failed")

    output = stream.getvalue()
    assert "424242" not in output
    assert "Shadow assessment mirror failed" in output
    assert "IntegrityError" in output


def test_database_error_passed_as_argument_is_redacted() -> None:
    logger, stream = _logger()
    error = _database_error()

    logger.error("Cause exception while process update id=%d\n%s: %s", 7, "IntegrityError", error)

    output = stream.getvalue()
    assert "424242" not in output
    assert "update id=7" in output


def test_other_errors_are_logged_normally() -> None:
    logger, stream = _logger()
    try:
        raise ValueError("visible detail")
    except ValueError:
        logger.exception("Unexpected failure")

    assert "visible detail" in stream.getvalue()


def test_database_error_passed_as_mapping_argument_is_redacted() -> None:
    logger, stream = _logger()

    logger.error("mapped error=%(error)s user=%(user)s", {"error": _database_error(), "user": 7})

    output = stream.getvalue()
    assert "424242" not in output
    assert "user=7" in output
