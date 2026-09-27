"""ORM models. Importing this package registers every model on ``Base.metadata``
(Alembic imports it so it can see the full schema)."""

from darwin.db.models.user_event import UserEvent

__all__ = ["UserEvent"]
