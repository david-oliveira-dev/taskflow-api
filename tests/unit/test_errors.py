"""The domain-error to HTTP-status mapping, offline."""

import pytest

from taskflow_api.api.errors import status_for
from taskflow_api.exceptions import (
    AuthenticationError,
    AuthorizationError,
    BusinessRuleError,
    ConfigurationError,
    ConflictError,
    InvalidCursorError,
    NotFoundError,
    TaskFlowError,
)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (NotFoundError("x"), 404),
        (ConflictError("x"), 409),
        (BusinessRuleError("x"), 422),
        (InvalidCursorError("x"), 422),
        (AuthorizationError("x"), 403),
        (AuthenticationError("x"), 401),
    ],
)
def test_each_domain_error_maps_to_its_status(error: TaskFlowError, expected: int) -> None:
    assert status_for(error) == expected


def test_an_unmapped_error_is_a_server_error() -> None:
    """A failure the edge does not recognise is this service's bug, not the client's.

    Answering 4xx would invite the caller to keep retrying a request that was never wrong.
    """
    assert status_for(ConfigurationError("missing secret")) == 500


def test_conflict_and_business_rule_are_not_confused() -> None:
    """The distinction the two exceptions exist to carry.

    409 says "change the state and try again"; 422 says "this request can never work".
    Collapsing them would make the API lie about which one the client is facing.
    """
    assert status_for(ConflictError("x")) != status_for(BusinessRuleError("x"))
