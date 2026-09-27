"""Application configuration.

Configuration is *data about the environment the code runs in* (which
environment, how verbose to log). It is kept separate from behaviour so the
same code runs unchanged locally, in tests, and in AWS — only the
environment variables differ.

Settings are read from process environment variables prefixed ``DARWIN_``.
No ``.env`` file is read here: loading one is the launcher's job
(``uv run --env-file``), which keeps tests independent of a developer's
local files.
"""

from typing import Literal

from pydantic import PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "dev", "prod"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# LOCAL ONLY default: the Homebrew PostgreSQL on this machine, no password in code
# (local connections are trusted by Homebrew's default setup). Real environments
# always set DARWIN_DATABASE_URL; on AWS it comes from Secrets Manager.
DEFAULT_DATABASE_URL = "postgresql+psycopg://darwin@localhost:5432/darwin_dev"
REQUIRED_DRIVER_SCHEME = "postgresql+psycopg"


class Settings(BaseSettings):
    """DarwinUX-owned settings. Provider credentials are added only when providers exist."""

    model_config = SettingsConfigDict(
        env_prefix="DARWIN_",
        # .env.example already lists future variables (e.g. DARWIN_DECIDER_ADAPTER);
        # ignore them until a field for them exists.
        extra="ignore",
        # `DARWIN_DATABASE_URL=` (empty, as in a copied template) means "not set":
        # use the default rather than failing validation on "".
        env_ignore_empty=True,
    )

    app_name: str = "DarwinUX"
    env: Environment = "local"
    log_level: LogLevel = "INFO"
    # One validated URL instead of host/port/user/password scattered around.
    database_url: PostgresDsn = PostgresDsn(DEFAULT_DATABASE_URL)

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        # Accept DARWIN_LOG_LEVEL=debug as well as DEBUG.
        return value.upper() if isinstance(value, str) else value

    @field_validator("database_url")
    @classmethod
    def _require_psycopg_driver(cls, value: PostgresDsn) -> PostgresDsn:
        # "postgresql://" would make SQLAlchemy look for psycopg2, which is not
        # installed. Fail at startup with a clear message instead.
        if value.scheme != REQUIRED_DRIVER_SCHEME:
            raise ValueError(f"database_url must use the {REQUIRED_DRIVER_SCHEME}:// scheme")
        return value
