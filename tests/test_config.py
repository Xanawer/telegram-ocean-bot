import pytest
from pydantic import ValidationError

from ocean_bot.config import Settings


def test_settings_normalize_postgres_url_for_async_sqlalchemy() -> None:
    settings = Settings(
        bot_token="123456:test-token",
        database_url="postgresql://user:pass@db/ocean",
        analytics_pepper="long-test-pepper",
        admin_user_id=42,
        privacy_contact="privacy@example.com",
    )

    assert settings.database_url == "postgresql+asyncpg://user:pass@db/ocean"
    assert settings.webhook_enabled is False


def test_webhook_requires_an_authentication_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(
            bot_token="123456:test-token",
            database_url="sqlite+aiosqlite:///local.db",
            analytics_pepper="long-test-pepper",
            admin_user_id=42,
            privacy_contact="privacy@example.com",
            webhook_base_url="https://example.com",
        )


@pytest.mark.parametrize(
    ("base_url", "secret"),
    [
        ("http://example.com", "x" * 32),
        ("https://example.com", "too-short"),
    ],
)
def test_webhook_requires_https_and_a_strong_secret(base_url: str, secret: str) -> None:
    with pytest.raises(ValidationError):
        Settings(
            bot_token="123456:test-token",
            database_url="sqlite+aiosqlite:///local.db",
            analytics_pepper="long-test-pepper",
            admin_user_id=42,
            privacy_contact="privacy@example.com",
            webhook_base_url=base_url,
            webhook_secret=secret,
        )


def test_external_retention_mode_disables_the_background_loop() -> None:
    settings = Settings(
        bot_token="123456:test-token",
        database_url="sqlite+aiosqlite:///local.db",
        analytics_pepper="long-test-pepper",
        admin_user_id=42,
        privacy_contact="privacy@example.com",
        retention_mode="external",
    )

    assert settings.background_retention_enabled is False


def test_external_webhook_setup_disables_startup_registration() -> None:
    settings = Settings(
        bot_token="123456:test-token",
        database_url="sqlite+aiosqlite:///local.db",
        analytics_pepper="long-test-pepper",
        admin_user_id=42,
        privacy_contact="privacy@example.com",
        webhook_base_url="https://bot.example.com",
        webhook_secret="s" * 32,
        webhook_setup_mode="external",
    )

    assert settings.configure_webhook_on_startup is False


def test_local_first_storage_requires_a_sqlite_database() -> None:
    settings = Settings(
        bot_token="123456:test-token",
        database_url="sqlite+aiosqlite:///remote.db",
        analytics_pepper="long-test-pepper",
        admin_user_id=42,
        privacy_contact="privacy@example.com",
        storage_mode="authoritative",
        local_database_url="sqlite+aiosqlite:///local.db",
    )

    assert settings.local_storage_enabled is True

    with pytest.raises(ValidationError):
        Settings(
            bot_token="123456:test-token",
            database_url="sqlite+aiosqlite:///remote.db",
            analytics_pepper="long-test-pepper",
            admin_user_id=42,
            privacy_contact="privacy@example.com",
            storage_mode="authoritative",
        )
