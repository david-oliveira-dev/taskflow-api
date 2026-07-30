"""Password hashing and token handling — pure functions, no database."""

import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from taskflow_api.config import Settings
from taskflow_api.exceptions import AuthenticationError
from taskflow_api.services.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


class TestPasswordHashing:
    def test_hash_verifies_against_its_own_password(self) -> None:
        hashed = hash_password("correct horse battery staple")

        assert verify_password("correct horse battery staple", hashed) is True

    def test_wrong_password_is_rejected(self) -> None:
        hashed = hash_password("correct horse battery staple")

        assert verify_password("Correct horse battery staple", hashed) is False

    def test_hash_is_salted(self) -> None:
        """Two identical passwords must not produce the same hash.

        Without a per-hash salt, identical hashes reveal which accounts share a password.
        """
        assert hash_password("same password") != hash_password("same password")

    def test_plaintext_never_appears_in_the_hash(self) -> None:
        assert "correct horse" not in hash_password("correct horse battery staple")

    def test_verifying_against_none_is_false(self) -> None:
        """The unknown-account path: it must fail, but it must still do the work."""
        assert verify_password("anything", None) is False

    def test_unknown_account_costs_about_as_much_as_a_wrong_password(self) -> None:
        """Guards the user-enumeration hole.

        If an unknown email returned early, failed logins would be measurably faster for
        addresses that are not registered — enough to enumerate accounts in bulk. Both
        paths run a real Argon2 verification, so they take a comparable time.

        The tolerance is wide on purpose: this is a shared machine, not a bench. A short
        circuit would show up as orders of magnitude, not as a few per cent.
        """
        hashed = hash_password("a real password for a real account")

        start = time.perf_counter()
        verify_password("wrong password", hashed)
        known = time.perf_counter() - start

        start = time.perf_counter()
        verify_password("wrong password", None)
        unknown = time.perf_counter() - start

        assert unknown > known / 10, (
            f"unknown-account path took {unknown:.4f}s against {known:.4f}s for a known "
            "account — it is short-circuiting, which leaks which emails are registered"
        )


class TestAccessTokens:
    def test_token_roundtrips(self, settings: Settings) -> None:
        user_id = uuid.uuid4()

        payload = decode_access_token(
            create_access_token(user_id=user_id, settings=settings), settings=settings
        )

        assert payload.subject == user_id

    def test_each_token_gets_its_own_jti(self, settings: Settings) -> None:
        """The claim that makes a revocation denylist possible later."""
        user_id = uuid.uuid4()

        first = decode_access_token(
            create_access_token(user_id=user_id, settings=settings), settings=settings
        )
        second = decode_access_token(
            create_access_token(user_id=user_id, settings=settings), settings=settings
        )

        assert first.token_id != second.token_id

    def test_expiry_follows_the_configured_ttl(self, settings: Settings) -> None:
        payload = decode_access_token(
            create_access_token(user_id=uuid.uuid4(), settings=settings), settings=settings
        )

        expected = datetime.now(UTC) + timedelta(minutes=settings.access_token_ttl_minutes)
        assert abs((payload.expires_at - expected).total_seconds()) < 5

    def test_expired_token_is_rejected(self, settings: Settings) -> None:
        expired = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "jti": str(uuid.uuid4()),
                "iat": datetime.now(UTC) - timedelta(hours=2),
                "exp": datetime.now(UTC) - timedelta(hours=1),
            },
            settings.jwt_secret.get_secret_value(),
            algorithm="HS256",
        )

        with pytest.raises(AuthenticationError, match="expired"):
            decode_access_token(expired, settings=settings)

    def test_token_signed_with_another_key_is_rejected(self, settings: Settings) -> None:
        forged = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "jti": str(uuid.uuid4()),
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(minutes=15),
            },
            # As long as the real key: rejection must come from the signature not
            # matching, not from the forged key being too short.
            "an-attacker-chosen-key-of-the-very-same-length",
            algorithm="HS256",
        )

        with pytest.raises(AuthenticationError):
            decode_access_token(forged, settings=settings)

    def test_unsigned_token_is_rejected(self, settings: Settings) -> None:
        """The classic JWT attack: strip the signature and set alg to none.

        Decoding with an explicit algorithm list is what stops it.
        """
        unsigned = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "jti": str(uuid.uuid4()),
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(minutes=15),
            },
            key="",
            algorithm="none",
        )

        with pytest.raises(AuthenticationError):
            decode_access_token(unsigned, settings=settings)

    @pytest.mark.parametrize("missing", ["sub", "exp", "iat", "jti"])
    def test_token_missing_a_required_claim_is_rejected(
        self, settings: Settings, missing: str
    ) -> None:
        claims: dict[str, object] = {
            "sub": str(uuid.uuid4()),
            "jti": str(uuid.uuid4()),
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(minutes=15),
        }
        del claims[missing]
        token = jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm="HS256")

        with pytest.raises(AuthenticationError):
            decode_access_token(token, settings=settings)

    def test_token_with_a_non_uuid_subject_is_rejected(self, settings: Settings) -> None:
        """A properly signed token whose claims are not what this service issues."""
        token = jwt.encode(
            {
                "sub": "not-a-uuid",
                "jti": str(uuid.uuid4()),
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(minutes=15),
            },
            settings.jwt_secret.get_secret_value(),
            algorithm="HS256",
        )

        with pytest.raises(AuthenticationError, match="malformed"):
            decode_access_token(token, settings=settings)

    @pytest.mark.parametrize("garbage", ["", "not.a.token", "a.b.c", "Bearer xyz"])
    def test_garbage_is_rejected_cleanly(self, settings: Settings, garbage: str) -> None:
        with pytest.raises(AuthenticationError):
            decode_access_token(garbage, settings=settings)
