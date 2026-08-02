"""Configuration parsing and its guarantees."""

import pytest
from pydantic import ValidationError

from taskflow_api.config import Settings, _mask_password

_SECRET = "a-distinctive-signing-key-of-sufficient-length"
_BASE = {
    "database_url": "postgresql+asyncpg://taskflow:hunter2@localhost:5432/taskflow",
    "redis_url": "redis://localhost:6379/0",
    "jwt_secret": _SECRET,
}


def _settings(**overrides: object) -> Settings:
    # _env_file=None is not a detail: without it these tests read whatever .env happens to
    # exist on the machine, and "does a missing value fail?" would pass or fail depending
    # on whether the developer had run the app locally.
    return Settings(_env_file=None, **{**_BASE, **overrides})  # type: ignore[arg-type]


def test_signing_key_is_not_exposed_in_repr() -> None:
    """The JWT key must not reach logs or tracebacks through a repr."""
    settings = _settings()

    # Asserting on the value, not on the word "secret" — the field is *named* jwt_secret.
    assert _SECRET not in repr(settings)
    assert settings.jwt_secret.get_secret_value() == _SECRET


def test_database_password_is_masked_for_logging() -> None:
    """PostgresDsn keeps the password in plain text; anything logged must not.

    Regression guard for a real leak: `repr(settings)` shows the DSN verbatim, so startup
    logging has to go through `safe_database_url`.
    """
    settings = _settings()

    assert "hunter2" not in settings.safe_database_url
    assert settings.safe_database_url == (
        "postgresql+asyncpg://taskflow:***@localhost:5432/taskflow"
    )


def test_masking_leaves_a_credential_free_dsn_alone() -> None:
    settings = _settings(redis_url="redis://localhost:6379/0")

    assert settings.safe_redis_url == "redis://localhost:6379/0"


def test_redis_password_is_masked_for_logging() -> None:
    # Pydantic normalises the DSN and fills in the default port, hence the :6379.
    settings = _settings(redis_url="redis://default:s3cr3t@cache/0")

    assert "s3cr3t" not in settings.safe_redis_url
    assert settings.safe_redis_url == "redis://default:***@cache:6379/0"


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("redis://default:s3cr3t@cache/0", "redis://default:***@cache/0"),
        ("postgresql://u:p@host:5432/db", "postgresql://u:***@host:5432/db"),
        ("postgresql://host:5432/db", "postgresql://host:5432/db"),
        ("postgresql://user@host/db", "postgresql://user@host/db"),
    ],
)
def test_mask_password_contract(dsn: str, expected: str) -> None:
    """The helper on its own, including shapes the DSN types normalise away.

    `RedisDsn` and `PostgresDsn` always fill in a default port, so a port-less DSN never
    reaches `safe_*_url` through them — but the helper still has to handle it.
    """
    assert _mask_password(dsn) == expected


def test_unknown_variable_is_rejected() -> None:
    """A typo in an env var should fail loudly, not be silently ignored."""
    with pytest.raises(ValidationError):
        _settings(typo_variable="x")


def test_missing_required_value_fails_at_construction() -> None:
    with pytest.raises(ValidationError):
        # Missing `database_url` on purpose — that is the point of the test, and it is
        # also what Pyright objects to.
        Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]  # see [tool.pyright] in pyproject.toml
            redis_url="redis://localhost:6379/0",
            jwt_secret=_SECRET,
        )


@pytest.mark.parametrize("ttl", [0, 61])
def test_token_ttl_is_bounded(ttl: int) -> None:
    """The expiry window is the only revocation mechanism, so it cannot be absurd."""
    with pytest.raises(ValidationError):
        _settings(access_token_ttl_minutes=ttl)


@pytest.mark.parametrize("weak", ["short", "x" * 31])
def test_signing_key_shorter_than_32_bytes_is_refused(weak: str) -> None:
    """RFC 7518 section 3.2: an HMAC key shorter than its hash weakens the signature.

    PyJWT only warns at runtime. Refusing to start is better than serving traffic with a
    signature that looks valid and is not.
    """
    with pytest.raises(ValidationError):
        _settings(jwt_secret=weak)


def test_is_production_reflects_environment() -> None:
    assert _settings(environment="production").is_production is True
    assert _settings(environment="local").is_production is False
