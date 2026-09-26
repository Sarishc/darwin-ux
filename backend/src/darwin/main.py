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

from darwin import __version__
from darwin.api.router import api_v1_router
from darwin.config import Settings
from darwin.logging_config import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Runs once at server startup (before ``yield``) and once at shutdown (after).

    Connections to external resources (database pools, clients) will be opened
    here in later steps — never at import time.
    """
    settings: Settings = app.state.settings
    app.state.started = True
    logger.info(
        "application started",
        extra={
            "context": {
                "app_name": settings.app_name,
                "version": __version__,
                "env": settings.env,
                "log_level": settings.log_level,
            }
        },
    )
    yield
    app.state.started = False
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
