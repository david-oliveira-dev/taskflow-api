"""The domain-error to status-and-code mapping, offline."""

import pytest

from taskflow_api.api.errors import classify
from taskflow_api.exceptions import (
    AuthenticationError,
    AuthorizationError,
    BusinessRuleError,
    ConfigurationError,
    ConflictError,
    IdempotencyInProgressError,
    IdempotencyKeyReusedError,
    InvalidCursorError,
    NotFoundError,
    TaskFlowError,
)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (NotFoundError("x"), (404, "not_found")),
        (ConflictError("x"), (409, "conflict")),
        (BusinessRuleError("x"), (422, "unprocessable")),
        (InvalidCursorError("x"), (422, "invalid_cursor")),
        (AuthorizationError("x"), (403, "forbidden")),
        (AuthenticationError("x"), (401, "unauthenticated")),
        (IdempotencyInProgressError("x"), (409, "idempotency_key_in_progress")),
        (IdempotencyKeyReusedError("x"), (422, "idempotency_key_reused")),
    ],
)
def test_each_domain_error_maps_to_its_status_and_code(
    error: TaskFlowError, expected: tuple[int, str]
) -> None:
    assert classify(error) == expected


def test_an_unmapped_error_is_a_server_error() -> None:
    """A failure the edge does not recognise is this service's bug, not the client's.

    Answering 4xx would invite the caller to keep retrying a request that was never wrong.
    """
    assert classify(ConfigurationError("missing secret")) == (500, "internal_error")


def test_conflict_and_business_rule_are_not_confused() -> None:
    """The distinction the two exceptions exist to carry.

    409 says "change the state and try again"; 422 says "this request can never work".
    Collapsing them would make the API lie about which one the client is facing.
    """
    assert classify(ConflictError("x"))[0] != classify(BusinessRuleError("x"))[0]


def test_the_two_conflicts_share_a_status_but_not_a_code() -> None:
    """A retryable in-flight replay and a genuine state conflict are both 409.

    The code is what lets a client tell "wait and retry" from "this will never work", which
    is exactly why the envelope carries one alongside the status.
    """
    in_flight = classify(IdempotencyInProgressError("x"))
    state = classify(ConflictError("x"))

    assert in_flight[0] == state[0] == 409
    assert in_flight[1] != state[1]
