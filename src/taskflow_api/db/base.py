"""Declarative base and the constraint naming convention.

The naming convention is not cosmetic. Without it PostgreSQL invents names for indexes,
unique constraints, check constraints and foreign keys, and Alembic then generates
downgrades that try to drop constraints by a name that differs between databases. The
acceptance criterion for this phase is that `downgrade base` runs clean, and that is only
reliably true when every constraint has a deterministic name.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for every ORM model in the project."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
