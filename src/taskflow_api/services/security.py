"""Password hashing and access tokens.

Pure functions over values: nothing here touches the database or a request, which is why
all of it is unit-testable without a container.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from taskflow_api.config import Settings
from taskflow_api.exceptions import AuthenticationError

_hasher = PasswordHasher()

# Argon2 hash of a value nobody can log in with. Used to spend the same CPU time when an
# email does not exist as when it does — see `verify_password`.
_DUMMY_HASH = _hasher.hash("a-password-that-belongs-to-no-account")


@dataclass(frozen=True, slots=True)
class TokenPayload:
    """The claims this service puts in, and reads back from, an access token."""

    subject: uuid.UUID
    token_id: uuid.UUID
    expires_at: datetime


def hash_password(plain: str) -> str:
    """Hash a password with Argon2id.

    Args:
        plain: The password as typed by the user.

    Returns:
        The encoded hash, including the parameters used, so they can be raised later
        without invalidating existing hashes.
    """
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str | None) -> bool:
    """Check a password against a hash.

    Args:
        plain: The candidate password.
        hashed: The stored hash, or None when no such account exists.

    Returns:
        True when the password matches.

    Note:
        Passing None is deliberate and is what closes the user-enumeration hole. Returning
        early for an unknown email would make failed logins measurably faster for addresses
        that are not registered, which is enough to enumerate accounts. Instead the same
        Argon2 verification runs against a dummy hash, so both paths cost the same.
    """
    try:
        _hasher.verify(hashed if hashed is not None else _DUMMY_HASH, plain)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return hashed is not None


def needs_rehash(hashed: str) -> bool:
    """Whether a stored hash was made with weaker parameters than the current ones.

    Lets the login path transparently upgrade hashes as the cost parameters are raised,
    instead of leaving old accounts on the settings of the day they signed up.
    """
    return _hasher.check_needs_rehash(hashed)


def create_access_token(*, user_id: uuid.UUID, settings: Settings) -> str:
    """Mint a signed access token.

    The `jti` is present even though nothing revokes tokens yet. It costs nothing now and
    is what makes a denylist a small change later rather than a token-format migration.
    """
    issued_at = datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=settings.access_token_ttl_minutes)
    payload = {
        "sub": str(user_id),
        "jti": str(uuid.uuid4()),
        "iat": issued_at,
        "exp": expires_at,
    }
    return jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str, *, settings: Settings) -> TokenPayload:
    """Verify a token and return its claims.

    Args:
        token: The bearer token from the request.
        settings: Supplies the key and the accepted algorithm.

    Returns:
        The decoded claims.

    Raises:
        AuthenticationError: The token is expired, tampered with, signed with the wrong
            key, or missing a claim this service requires.
    """
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            # A list of exactly one algorithm, never `None`: accepting whatever the token's
            # header asks for is how "alg": "none" attacks work.
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "exp", "iat", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Access token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Access token is not valid.") from exc

    try:
        return TokenPayload(
            subject=uuid.UUID(claims["sub"]),
            token_id=uuid.UUID(claims["jti"]),
            expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC),
        )
    except (KeyError, TypeError, ValueError) as exc:
        # A structurally valid token whose claims are not what we put there.
        raise AuthenticationError("Access token claims are malformed.") from exc
