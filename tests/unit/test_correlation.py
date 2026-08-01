"""Correlation id resolution, offline."""

import uuid

import pytest

from taskflow_api.api.correlation import _resolve, correlation_id


class TestResolve:
    def test_a_well_formed_inbound_id_is_kept(self) -> None:
        """So a trace survives across services instead of restarting at each hop."""
        assert _resolve("abc-123_XYZ.4") == "abc-123_XYZ.4"

    def test_a_missing_id_is_minted(self) -> None:
        minted = _resolve(None)

        # Parses as a UUID, so it is unique per request rather than a shared constant.
        assert uuid.UUID(minted)

    @pytest.mark.parametrize(
        "supplied",
        [
            "",
            "has space",
            "line\nbreak",
            "semi;colon",
            'quote"',
            "x" * 65,
            "unicode-ç",
        ],
    )
    def test_an_unsafe_inbound_id_is_replaced(self, supplied: str) -> None:
        """The id is echoed into responses and log lines.

        Accepting arbitrary client input there is how log forging starts: a newline lets a
        caller write what looks like a second log entry of their own.
        """
        resolved = _resolve(supplied)

        assert resolved != supplied
        assert uuid.UUID(resolved)


def test_outside_a_request_the_id_is_empty() -> None:
    """No ambient default that could be mistaken for a real request's id."""
    assert correlation_id() == ""
