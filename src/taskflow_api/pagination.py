"""Keyset (cursor) pagination.

Offset pagination is the obvious choice and the wrong one here. `LIMIT 20 OFFSET 10000`
makes PostgreSQL walk and discard ten thousand rows, and — worse — the window shifts when
rows are inserted or deleted between requests, so a client paging through a list silently
**skips or repeats** entries. Keyset pagination asks "give me the rows after this exact
position", which is stable under concurrent writes and uses the index directly.

The cursor is opaque by design: it is base64 so clients treat it as a token instead of
building one by hand, not because the contents are secret.
"""

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime

from taskflow_api.exceptions import InvalidCursorError

_SEPARATOR = "|"

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class CursorPosition:
    """The exact row a page continues from."""

    created_at: datetime
    entity_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of results, plus how to ask for the next one."""

    items: list[T]
    next_cursor: str | None
    has_more: bool


def encode_cursor(position: CursorPosition) -> str:
    """Serialise a position into an opaque token.

    Args:
        position: The row the next page should continue after.

    Returns:
        A URL-safe base64 token.
    """
    raw = f"{position.created_at.isoformat()}{_SEPARATOR}{position.entity_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> CursorPosition:
    """Parse a token produced by `encode_cursor`.

    Args:
        cursor: The opaque token supplied by the client.

    Returns:
        The position it encodes.

    Raises:
        InvalidCursorError: The token is malformed. Cursors come straight from user input,
            so every failure mode here is reachable by anyone and must produce a clean 4xx
            rather than an unhandled exception.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        timestamp, _, entity_id = raw.partition(_SEPARATOR)
        if not timestamp or not entity_id:
            raise InvalidCursorError("Cursor is missing one of its two components.")
        return CursorPosition(
            created_at=datetime.fromisoformat(timestamp),
            entity_id=uuid.UUID(entity_id),
        )
    except InvalidCursorError:
        raise
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursorError("Cursor is not a valid pagination token.") from exc


def clamp_page_size(limit: int | None) -> int:
    """Bound a client-supplied page size.

    An unbounded `limit` is a denial-of-service vector: one request asking for a million
    rows costs the database far more than it costs the caller.

    Args:
        limit: The requested size, or None for the default.

    Returns:
        A size within [1, MAX_PAGE_SIZE].
    """
    if limit is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(limit, MAX_PAGE_SIZE))
