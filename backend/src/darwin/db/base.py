"""Declarative base shared by all ORM models.

``Base.metadata`` is the Python description of the schema. Alembic compares
it with the real database to help write migrations; the application itself
never uses it to create tables.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint names. Without them PostgreSQL invents names, and
# a later migration that must drop/alter a constraint can't refer to it reliably.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
