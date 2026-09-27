"""Sessions: one unit of work per request.

- The *session factory* (``sessionmaker``) is created once, bound to the engine.
- Each request gets its **own** Session from ``get_session`` and it is always
  closed afterwards, returning its connection to the pool.
- A Session does not commit on its own. Code that changes data owns the
  transaction explicitly::

      def handler(session: DbSession) -> ...:
          with session.begin():          # BEGIN
              session.add(obj)
          # COMMIT here, or ROLLBACK if the block raised
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine)


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency: a request-scoped Session."""
    session_factory: sessionmaker[Session] = request.app.state.session_factory
    with session_factory() as session:
        yield session


DbSession = Annotated[Session, Depends(get_session)]
