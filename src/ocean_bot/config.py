from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="OCEAN_",
        extra="ignore",
    )

    bot_token: SecretStr
    database_url: str
    analytics_pepper: SecretStr
    admin_user_id: int
    privacy_contact: str
    webhook_base_url: str | None = None
    webhook_secret: SecretStr | None = None
    port: int = 8080
    retention_mode: Literal["background", "external"] = "background"
    webhook_setup_mode: Literal["startup", "external"] = "startup"
    storage_mode: Literal["remote", "shadow", "authoritative"] = "remote"
    local_database_url: str | None = None

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: object) -> object:
        if isinstance(value, str) and value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value

    @field_validator("webhook_base_url", "webhook_secret", "local_database_url", mode="before")
    @classmethod
    def blank_means_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_webhook(self) -> Settings:
        if self.webhook_base_url:
            parsed_url = urlsplit(self.webhook_base_url)
            if parsed_url.scheme != "https" or not parsed_url.netloc:
                raise ValueError("OCEAN_WEBHOOK_BASE_URL must be a valid HTTPS URL")
            if self.webhook_secret is None:
                raise ValueError("OCEAN_WEBHOOK_SECRET is required when webhooks are enabled")
            if len(self.webhook_secret.get_secret_value()) < 32:
                raise ValueError("OCEAN_WEBHOOK_SECRET must contain at least 32 characters")
        if len(self.analytics_pepper.get_secret_value()) < 16:
            raise ValueError("OCEAN_ANALYTICS_PEPPER must contain at least 16 characters")
        if self.storage_mode != "remote":
            if self.local_database_url is None:
                raise ValueError("OCEAN_LOCAL_DATABASE_URL is required for local storage modes")
            if not self.local_database_url.startswith("sqlite+aiosqlite:///"):
                raise ValueError("OCEAN_LOCAL_DATABASE_URL must use sqlite+aiosqlite")
        return self

    @property
    def webhook_enabled(self) -> bool:
        return self.webhook_base_url is not None

    @property
    def background_retention_enabled(self) -> bool:
        return self.retention_mode == "background"

    @property
    def configure_webhook_on_startup(self) -> bool:
        return self.webhook_setup_mode == "startup"

    @property
    def local_storage_enabled(self) -> bool:
        return self.storage_mode != "remote"
