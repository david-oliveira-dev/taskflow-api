"""Query helpers shared by every repository.

Keyset pagination is the same three steps everywhere — narrow to the rows after a
position, fetch one more than asked for, turn the last kept row back into a cursor. Written
once here so that a repository cannot get the "one extra row" trick subtly wrong and start
reporting `has_more` incorrectly at page boundaries.
"""

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import ColumnElement, Select, SQLColumnExpression, literal, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from taskflow_api.pagination import CursorPosition, Page, encode_cursor


def after_position(
    created_at: SQLColumnExpression[datetime],
    entity_id: SQLColumnExpression[uuid.UUID],
    after: CursorPosition,
) -> ColumnElement[bool]:
    """Build the "rows strictly after this cursor" predicate, for a descending sort.

    A row comparison rather than two chained conditions: `(created_at, id) < (:ts, :id)` is
    what PostgreSQL can answer straight from a composite index, and it keeps the ordering
    total. Comparing only `created_at` would drop or duplicate rows sharing a timestamp,
    which is exactly the failure keyset pagination exists to avoid.

    Args:
        created_at: The timestamp column the listing is ordered by.
        entity_id: The tie-breaking id column.
        after: The position the previous page ended at.

    Returns:
        A boolean SQL expression.
    """
    return tuple_(created_at, entity_id) < tuple_(
        literal(after.created_at), literal(after.entity_id)
    )


async def paginate[T](
    session: AsyncSession,
    stmt: Select[tuple[T]],
    *,
    limit: int,
    position: Callable[[T], CursorPosition],
) -> Page[T]:
    """Run a keyset query and package the result.

    Fetches `limit + 1` rows so that "is there a next page?" is answered by the row count
    itself. The obvious alternative, a second `COUNT(*)`, makes the database walk the whole
    filtered set on every page request just to produce a boolean.

    Args:
        session: The session to execute on.
        stmt: A statement already ordered and filtered, but **not** limited.
        limit: How many rows the caller asked for, already clamped.
        position: Turns a row into the cursor position that follows it.

    Returns:
        The page, with a cursor only when more rows exist.
    """
    result = await session.execute(stmt.limit(limit + 1))
    rows = list(result.scalars().all())

    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = encode_cursor(position(items[-1])) if has_more and items else None
    return Page(items=items, next_cursor=next_cursor, has_more=has_more)
