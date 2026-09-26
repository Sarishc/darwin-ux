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

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "dev", "prod"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """DarwinUX-owned settings. Provider credentials are added only when providers exist."""

    model_config = SettingsConfigDict(
        env_prefix="DARWIN_",
        # .env.example already lists future variables (e.g. DARWIN_DATABASE_URL);
        # ignore them until a field for them exists.
        extra="ignore",
    )

    app_name: str = "DarwinUX"
    env: Environment = "local"
    log_level: LogLevel = "INFO"

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        # Accept DARWIN_LOG_LEVEL=debug as well as DEBUG.
        return value.upper() if isinstance(value, str) else value
