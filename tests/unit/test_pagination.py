"""Cursor encoding and page-size clamping — pure logic, no database."""

import base64
import uuid
from datetime import UTC, datetime

import pytest

from taskflow_api.exceptions import InvalidCursorError
from taskflow_api.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    CursorPosition,
    clamp_page_size,
    decode_cursor,
    encode_cursor,
)


def test_cursor_roundtrips() -> None:
    position = CursorPosition(
        created_at=datetime(2026, 7, 30, 12, 34, 56, 789000, tzinfo=UTC),
        entity_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
    )

    assert decode_cursor(encode_cursor(position)) == position


def test_cursor_preserves_the_timezone() -> None:
    """A naive timestamp would compare wrongly against a timestamptz column."""
    position = CursorPosition(
        created_at=datetime(2026, 7, 30, 12, 0, tzinfo=UTC), entity_id=uuid.uuid4()
    )

    assert decode_cursor(encode_cursor(position)).created_at.tzinfo is not None


@pytest.mark.parametrize(
    ("cursor", "reason"),
    [
        ("not-base64!!", "not base64"),
        (base64.urlsafe_b64encode(b"missing-separator").decode(), "no separator"),
        (base64.urlsafe_b64encode(b"|").decode(), "both parts empty"),
        (
            base64.urlsafe_b64encode(b"not-a-date|" + str(uuid.uuid4()).encode()).decode(),
            "bad timestamp",
        ),
        (base64.urlsafe_b64encode(b"2026-07-30T12:00:00+00:00|not-a-uuid").decode(), "bad uuid"),
        ("", "empty"),
    ],
)
def test_malformed_cursor_raises_a_domain_error(cursor: str, reason: str) -> None:
    """Cursors are user input: every one of these is reachable by anyone.

    They must produce a clean domain error the API can turn into a 4xx, never an unhandled
    ValueError surfacing as a 500.
    """
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor)


def test_page_size_defaults_when_unspecified() -> None:
    assert clamp_page_size(None) == DEFAULT_PAGE_SIZE


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(1, 1), (50, 50), (MAX_PAGE_SIZE, MAX_PAGE_SIZE)],
)
def test_page_size_within_bounds_is_kept(requested: int, expected: int) -> None:
    assert clamp_page_size(requested) == expected


@pytest.mark.parametrize("requested", [MAX_PAGE_SIZE + 1, 1_000_000])
def test_oversized_page_is_capped(requested: int) -> None:
    """An unbounded limit is a denial-of-service vector, not a convenience."""
    assert clamp_page_size(requested) == MAX_PAGE_SIZE


@pytest.mark.parametrize("requested", [0, -1, -1000])
def test_nonpositive_page_size_becomes_one(requested: int) -> None:
    assert clamp_page_size(requested) == 1
