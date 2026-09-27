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

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["local", "test", "dev", "prod"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# LOCAL ONLY default: the Homebrew PostgreSQL on this machine, no password in code
# (local connections are trusted by Homebrew's default setup). Real environments
# always set DARWIN_DATABASE_URL; on AWS it comes from Secrets Manager.
DEFAULT_DATABASE_URL = "postgresql+psycopg://darwin@localhost:5432/darwin_dev"
REQUIRED_DRIVER_SCHEME = "postgresql+psycopg"

# Browser origins allowed to call the API (the Next.js dev server, Step 7).
DEFAULT_CORS_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]


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

    # Telemetry queue + worker (Step 6). Local defaults; see docs/DATA_PIPELINES.md.
    # How long a received message stays invisible to other workers (the lease).
    queue_visibility_timeout_seconds: float = Field(default=30.0, gt=0)
    # Deliveries before a message is dead-lettered.
    queue_max_attempts: int = Field(default=5, ge=1)
    # How long an idle worker sleeps before polling again.
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0)

    # Explicit browser origins for CORS: DARWIN_CORS_ORIGINS=a,b (comma-separated).
    # Never "*". An empty value means "use the local defaults".
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_CORS_ORIGINS)
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        # Accept DARWIN_LOG_LEVEL=debug as well as DEBUG.
        return value.upper() if isinstance(value, str) else value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("cors_origins")
    @classmethod
    def _require_exact_origins(cls, origins: list[str]) -> list[str]:
        # An origin is scheme://host[:port] — no wildcard, path, query, or trailing slash.
        for origin in origins:
            parts = urlsplit(origin)
            if (
                "*" in origin
                or parts.scheme not in ("http", "https")
                or not parts.netloc
                or origin != f"{parts.scheme}://{parts.netloc}"
            ):
                raise ValueError(f"invalid CORS origin: {origin!r} (use scheme://host[:port])")
        return origins

    @field_validator("database_url")
    @classmethod
    def _require_psycopg_driver(cls, value: PostgresDsn) -> PostgresDsn:
        # "postgresql://" would make SQLAlchemy look for psycopg2, which is not
        # installed. Fail at startup with a clear message instead.
        if value.scheme != REQUIRED_DRIVER_SCHEME:
            raise ValueError(f"database_url must use the {REQUIRED_DRIVER_SCHEME}:// scheme")
        return value
