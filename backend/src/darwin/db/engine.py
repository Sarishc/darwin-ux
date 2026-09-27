"""The SQLAlchemy engine: connectivity and the connection pool.

An Engine does not open a connection when it is created. It opens (and pools)
connections the first time something asks for one. That is why the API can
start while PostgreSQL is down and report "not ready" instead of crashing.

Create one engine per process (in the FastAPI lifespan), never per request.
"""

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# Fail fast when the server is unreachable, instead of hanging readiness probes.
CONNECT_TIMEOUT_SECONDS = 2


def create_db_engine(database_url: str) -> Engine:
    return create_engine(
        database_url,
        # Test each pooled connection before use; transparently replaces
        # connections that died (e.g. PostgreSQL was restarted).
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )


def database_is_available(engine: Engine) -> bool:
    """Cheapest possible end-to-end probe: get a connection and run ``SELECT 1``."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return False
    return True
