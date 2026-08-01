"""Idempotency for POST, serialised by the database.

Storing the response is the easy half. The case that actually breaks services is **two
requests with the same key arriving together**, the first still in flight — and that one is
not solved by caching, it is solved by making one of them lose a race deterministically.

The mechanism is the composite primary key on `(user_id, key)`. The second request loses its
`INSERT`, and that is what serialises the pair. Checking with a `SELECT` first and inserting
after leaves a window in which both callers see nothing and both proceed: exactly the
duplicate the header exists to prevent.

**The reservation runs in its own transaction**, separate from the request's. That is not an
optimisation, it is the difference between the two behaviours the specification asks for. If
the reservation shared the request transaction, the second caller's `INSERT` would *block*
on the unique index until the first request finished all of its work — so a replay would
wait instead of being told 409 immediately. Committing the reservation first makes the
conflict visible at once.

States, and what each produces:

- no row              -> reserve, run the handler, store the outcome
- row, `in_progress`  -> 409; do not wait, do not reprocess
- row, `completed`    -> replay the stored status and body
- row, different hash -> 422; returning the first request's response to a different payload
  is worse than failing, because the caller would believe its own body had been applied
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from taskflow_api.db.models import IdempotencyKey
from taskflow_api.enums import IdempotencyStatus
from taskflow_api.exceptions import IdempotencyInProgressError, IdempotencyKeyReusedError

#: How long a recorded response stays replayable. Without an expiry the table only grows.
RETENTION = timedelta(hours=24)

MAX_KEY_LENGTH = 255

# The envelope promises `message` is prose a person can read. Raising with the bare key put
# the key itself there, which told the caller nothing about what went wrong.
IN_PROGRESS_MESSAGE = (
    "A request with this Idempotency-Key is still being processed. Retry once it completes."
)
REUSED_MESSAGE = (
    "This Idempotency-Key was already used with a different request body. "
    "Use a new key for a different request."
)


def fingerprint(body: bytes) -> str:
    """Hash a request body so a replay with different content can be detected.

    SHA-256 over the raw bytes: this compares payloads, it does not protect them, so the
    choice is about collision resistance rather than secrecy.
    """
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True, slots=True)
class Reservation:
    """A key this request owns, and must finish by recording an outcome."""

    user_id: uuid.UUID
    key: str


# N818: control flow, not a failure. It is an exception because raising is the only way a
# FastAPI dependency can stop the endpoint from running — and stopping it is the whole
# point. Signalling by any other means leaves the handler to create the resource a second
# time, which is precisely the bug this exists to prevent.
class IdempotentReplay(Exception):  # noqa: N818
    """Raised to short-circuit a request whose key has already completed."""

    def __init__(self, replay: "Replay") -> None:
        super().__init__(f"replaying stored response {replay.status_code}")
        self.replay = replay


@dataclass(frozen=True, slots=True)
class Replay:
    """A previously completed response, to be returned verbatim."""

    status_code: int
    payload: dict[str, object] | None


class IdempotencyStore:
    """Reserves keys and records their outcomes, each in its own transaction.

    Takes a session **factory**, not a session: every method here opens and commits its own
    unit of work, deliberately outside whatever transaction the request is running.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def reserve(
        self, *, user_id: uuid.UUID, key: str, request_hash: str, now: datetime | None = None
    ) -> Reservation | Replay:
        """Claim a key, or report what already happened under it.

        Args:
            user_id: The authenticated caller. Keys are scoped per account so one client
                cannot collide with — or probe — another's.
            key: The `Idempotency-Key` header value.
            request_hash: `fingerprint()` of the request body.
            now: Injectable clock, so expiry is testable without waiting a day.

        Returns:
            A `Reservation` when this request owns the key, or a `Replay` of the stored
            response when the key already completed.

        Raises:
            IdempotencyInProgressError: The key is in flight.
            IdempotencyKeyReusedError: The key was used with a different body.
        """
        moment = now or datetime.now(UTC)

        async with self._sessions() as session:
            record = IdempotencyKey(
                user_id=user_id,
                key=key,
                request_hash=request_hash,
                status=IdempotencyStatus.IN_PROGRESS,
                expires_at=moment + RETENTION,
            )
            session.add(record)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
            else:
                return Reservation(user_id=user_id, key=key)

            existing = await self._load(session, user_id=user_id, key=key)

        if existing is None:
            # The row lost the insert race and then vanished before this read — the previous
            # owner failed and released it. Treating that as a conflict asks the client to
            # retry, which is exactly the right move and needs no special case.
            raise IdempotencyInProgressError(IN_PROGRESS_MESSAGE)

        stored, status_value, snapshot, status_code, expires_at = existing

        if expires_at <= moment:
            # Expired but not yet swept. Take it over rather than answering from a record
            # the client was told it could stop relying on.
            await self._reclaim(user_id=user_id, key=key, request_hash=request_hash, now=moment)
            return Reservation(user_id=user_id, key=key)

        if stored != request_hash:
            raise IdempotencyKeyReusedError(REUSED_MESSAGE)

        if status_value == IdempotencyStatus.IN_PROGRESS:
            raise IdempotencyInProgressError(IN_PROGRESS_MESSAGE)

        return Replay(status_code=status_code or 200, payload=snapshot)

    async def complete(
        self,
        reservation: Reservation,
        *,
        status_code: int,
        payload: dict[str, object] | None,
    ) -> None:
        """Record the outcome so a later replay can be answered without reprocessing."""
        async with self._sessions() as session:
            record = await session.get(IdempotencyKey, (reservation.user_id, reservation.key))
            if record is None:  # pragma: no cover - only if swept mid-request
                return
            record.status = IdempotencyStatus.COMPLETED
            record.status_code = status_code
            record.response_snapshot = payload
            await session.commit()

    async def release(self, reservation: Reservation) -> None:
        """Drop a reservation whose request did not succeed.

        Without this a failed request would hold its key `in_progress` until the retention
        window elapsed, and every retry — including the one that would have worked — would
        be answered 409 for a day.
        """
        async with self._sessions() as session:
            record = await session.get(IdempotencyKey, (reservation.user_id, reservation.key))
            if record is not None:
                await session.delete(record)
                await session.commit()

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        """Delete records past their retention window.

        Returns:
            How many rows went. Exposed as a method rather than a background loop so the
            schedule stays an operational decision — cron, a worker, or a test.
        """
        moment = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                select(IdempotencyKey).where(IdempotencyKey.expires_at <= moment)
            )
            records = list(result.scalars().all())
            for record in records:
                await session.delete(record)
            await session.commit()
            return len(records)

    async def _load(
        self, session: AsyncSession, *, user_id: uuid.UUID, key: str
    ) -> tuple[str, IdempotencyStatus, dict[str, object] | None, int | None, datetime] | None:
        record = await session.get(IdempotencyKey, (user_id, key))
        if record is None:
            return None
        return (
            record.request_hash,
            record.status,
            record.response_snapshot,
            record.status_code,
            record.expires_at,
        )

    async def _reclaim(
        self, *, user_id: uuid.UUID, key: str, request_hash: str, now: datetime
    ) -> None:
        async with self._sessions() as session:
            record = await session.get(IdempotencyKey, (user_id, key))
            if record is None:  # pragma: no cover - swept between the read and here
                return
            record.request_hash = request_hash
            record.status = IdempotencyStatus.IN_PROGRESS
            record.response_snapshot = None
            record.status_code = None
            record.expires_at = now + RETENTION
            await session.commit()
