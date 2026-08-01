"""Configuration, read once from the environment.

Every setting comes from an environment variable and is documented in `.env.example`
(general standard, section 3). Settings are validated at import of the application, so a
missing or malformed value stops the process at startup instead of failing later on the
first request that needs it.
"""

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    environment: Literal["local", "ci", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    database_url: PostgresDsn = Field(
        description="Async SQLAlchemy DSN, e.g. postgresql+asyncpg://user:pass@host:5432/db",
    )
    redis_url: RedisDsn = Field(description="Redis DSN used for rate limiting.")

    # Secret, never logged: SecretStr keeps it out of reprs and structured log payloads.
    # The 32-byte floor is RFC 7518 section 3.2: an HMAC key shorter than the hash it feeds
    # weakens the signature, and PyJWT warns about it at runtime. Better to refuse to start
    # than to serve traffic with a signature that looks fine and is not.
    jwt_secret: SecretStr = Field(
        min_length=32, description="HMAC key for signing access tokens (>= 32 bytes)."
    )
    jwt_algorithm: Literal["HS256"] = "HS256"

    # Rate limiting. Generous by default: the point is to stop a runaway client, not to
    # shape ordinary traffic, and a limit tight enough to trip in normal use trains people
    # to ignore 429s.
    rate_limit_requests: int = Field(default=120, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)
    # Short-lived on purpose: this service has no refresh token and no revocation, so the
    # expiry window *is* the security boundary. See docs/adr/ (JWT decision).
    access_token_ttl_minutes: int = Field(default=15, ge=1, le=60)

    @property
    def is_production(self) -> bool:
        """Whether the service runs with production guarantees (docs off, debug off)."""
        return self.environment == "production"

    @property
    def safe_database_url(self) -> str:
        """The database DSN with its password masked, safe to log.

        `PostgresDsn` does *not* mask credentials in its repr, so logging a `Settings`
        object — or the DSN itself — would put the database password in the log stream.
        Anything that reports configuration must use this instead.
        """
        return _mask_password(str(self.database_url))

    @property
    def safe_redis_url(self) -> str:
        """The Redis DSN with its password masked, safe to log."""
        return _mask_password(str(self.redis_url))


def _mask_password(dsn: str) -> str:
    """Replace the password in a DSN with a fixed placeholder.

    Args:
        dsn: A URL-shaped connection string, with or without credentials.

    Returns:
        The same string with the password replaced, or unchanged if there is none.
    """
    parsed = urlsplit(dsn)
    if parsed.password is None:
        return dsn

    userinfo = f"{parsed.username or ''}:***"
    netloc = f"{userinfo}@{parsed.hostname or ''}"
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit(parsed._replace(netloc=netloc))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, parsed once.

    Cached because parsing re-reads `.env` and re-validates every field; the FastAPI
    dependency graph would otherwise do that on every single request.
    """
    return Settings()
