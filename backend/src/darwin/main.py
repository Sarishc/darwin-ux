"""Application entrypoint: builds the FastAPI (ASGI) application.

Uvicorn (the ASGI server) imports ``darwin.main:app``, accepts HTTP
connections, and hands each request to ``app``. FastAPI then matches the path
to a router, calls the handler, validates the returned Pydantic model, and
serialises it to JSON.

Dependency direction: ``main`` imports ``api`` and ``config``; they never
import ``main``.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import make_url

from darwin import __version__
from darwin.api.router import api_v1_router
from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.session import create_session_factory
from darwin.logging_config import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Runs once at server startup (before ``yield``) and once at shutdown (after).

    The database engine (and its connection pool) lives exactly as long as the
    application. Creating it does not connect, so startup succeeds even if
    PostgreSQL is down; readiness reports that instead. The schema is NOT
    created here — that is Alembic's job.
    """
    settings: Settings = app.state.settings
    engine = create_db_engine(str(settings.database_url))
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.started = True
    database = make_url(str(settings.database_url))
    logger.info(
        "application started",
        extra={
            "context": {
                "app_name": settings.app_name,
                "version": __version__,
                "env": settings.env,
                "log_level": settings.log_level,
                # Where, never how: no user or password in logs.
                "database": f"{database.host}:{database.port}/{database.database}",
            }
        },
    )
    yield
    app.state.started = False
    engine.dispose()
    logger.info("application stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a new application. Tests pass explicit settings; production reads the environment."""
    settings = settings if settings is not None else Settings()
    configure_logging(settings.log_level)

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.state.settings = settings
    # Becomes True when the lifespan startup has run; readiness depends on it.
    app.state.started = False
    app.include_router(api_v1_router)
    return app


# Module-level instance so `uvicorn darwin.main:app` can find it.
app = create_app()
