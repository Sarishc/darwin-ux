"""Alembic migration environment.

The database URL comes from the application's own settings
(``DARWIN_DATABASE_URL`` via ``darwin.config.Settings``), so migrations and the
app can never silently point at different databases. Code that runs Alembic
programmatically (the integration tests) may pass an explicit URL through
``config.attributes["database_url"]``.

Modes:
- online  (default):        connect and apply migrations.
- offline (``--sql`` flag):  print the SQL (DDL) instead of running it.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from darwin.config import Settings
from darwin.db import models  # noqa: F401  (registers every model on Base.metadata)
from darwin.db.base import Base

config = context.config

# Use alembic.ini's logging only when run from the CLI, and never disable the
# application's loggers.
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# The schema as described by the ORM models; used by `alembic revision --autogenerate`.
target_metadata = Base.metadata


def database_url() -> str:
    explicit = config.attributes.get("database_url")
    return str(explicit) if explicit else str(Settings().database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # NullPool: a migration run is a short-lived script; no pool needed.
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
