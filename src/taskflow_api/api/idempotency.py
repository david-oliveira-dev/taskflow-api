"""Wiring idempotency into the request cycle.

The work is split across two places, because the two halves need different things:

- a **dependency** claims the key. It runs after authentication, which is what makes the
  caller's id available — keys are scoped per account.
- the **route class** records the outcome, after the endpoint has produced its response and
  before Starlette sends it. Doing this in a dependency's teardown would repeat the mistake
  ADR 0006 documents: the client would be told the request succeeded before the key was
  marked completed, so an immediate retry could be processed a second time.
"""

import json
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request, Response, status

from taskflow_api.api.deps import CurrentUserDep
from taskflow_api.api.routing import TransactionalRoute
from taskflow_api.exceptions import IdempotencyKeyReusedError
from taskflow_api.services.idempotency import (
    MAX_KEY_LENGTH,
    IdempotencyStore,
    IdempotentReplay,
    Replay,
    Reservation,
    fingerprint,
)

HEADER_NAME = "Idempotency-Key"

#: Set on `request.state` when this request owns a key, so the route class can finish it.
STATE_ATTRIBUTE = "idempotency_reservation"


async def claim_idempotency_key(
    request: Request,
    user: CurrentUserDep,
    idempotency_key: Annotated[str | None, Header(alias=HEADER_NAME)] = None,
) -> None:
    """Reserve the caller's key, or arrange for the stored response to be replayed.

    Optional by design: a request without the header behaves exactly as before. Requiring it
    would break every existing client to protect the ones that retry.

    Raises:
        IdempotencyKeyReusedError: The header is empty or too long, or names a key already
            used with a different body. Both statuses come from the central error mapping.
    """
    if idempotency_key is None:
        return

    if not idempotency_key.strip() or len(idempotency_key) > MAX_KEY_LENGTH:
        raise IdempotencyKeyReusedError(
            f"{HEADER_NAME} must be non-empty and at most {MAX_KEY_LENGTH} characters."
        )

    store: IdempotencyStore = request.app.state.idempotency
    outcome = await store.reserve(
        user_id=user.id,
        key=idempotency_key,
        request_hash=fingerprint(await request.body()),
    )

    if isinstance(outcome, Replay):
        # Raised, not recorded: the endpoint must not run. A dependency has no other way to
        # stop it, and letting it run would create the resource a second time.
        raise IdempotentReplay(outcome)

    setattr(request.state, STATE_ATTRIBUTE, outcome)


IdempotencyDep = Depends(claim_idempotency_key)


class IdempotentRoute(TransactionalRoute):
    """A transactional route that also records the outcome of an idempotent request."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the handler so a completed key is recorded once the response exists."""
        handle = super().get_route_handler()

        async def handler(request: Request) -> Response:
            store: IdempotencyStore = request.app.state.idempotency
            try:
                response = await handle(request)
            except Exception:
                # The handler failed, so the key must not stay claimed — otherwise every
                # retry, including the one that would work, is answered 409 for a day.
                claimed: Reservation | None = getattr(request.state, STATE_ATTRIBUTE, None)
                if claimed is not None:
                    await store.release(claimed)
                raise

            reservation: Reservation | None = getattr(request.state, STATE_ATTRIBUTE, None)
            if reservation is None:
                return response

            if response.status_code >= status.HTTP_400_BAD_REQUEST:
                await store.release(reservation)
                return response

            await store.complete(
                reservation,
                status_code=response.status_code,
                payload=_decode(response),
            )
            return response

        return handler


def _decode(response: Response) -> dict[str, object] | None:
    """Read a JSON response body back so it can be stored for replay.

    Returns None for anything that is not a JSON object — a 204 has no body, and storing a
    fragment we cannot faithfully reproduce would make the replay differ from the original.
    """
    body = getattr(response, "body", None)
    if not body:
        return None
    try:
        decoded = json.loads(body)
    except (ValueError, TypeError):  # pragma: no cover - responses here are always JSON
        return None
    return decoded if isinstance(decoded, dict) else None


def replay_response(replay: Replay) -> Response:
    """Rebuild a stored response.

    `Idempotent-Replay` is set so a client can tell a cached answer from a fresh one —
    useful when debugging a retry loop, and honest about what happened.
    """
    return Response(
        status_code=replay.status_code,
        content=json.dumps(replay.payload) if replay.payload is not None else None,
        media_type="application/json",
        headers={"Idempotent-Replay": "true"},
    )


def install_replay_handler(app: FastAPI) -> None:
    """Turn the replay signal into the stored response.

    Registered separately from the error handlers because a replay is not an error: it is
    the header working exactly as intended, and it answers with the original 2xx.
    """

    @app.exception_handler(IdempotentReplay)
    async def _replay(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, IdempotentReplay)
        return replay_response(exc.replay)
