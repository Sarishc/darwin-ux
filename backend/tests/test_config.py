import pytest
from pydantic import ValidationError

from darwin.config import DEFAULT_DATABASE_URL, Settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Make every test start from an empty DARWIN_* environment.
    for name in ("DARWIN_APP_NAME", "DARWIN_ENV", "DARWIN_LOG_LEVEL", "DARWIN_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_allow_starting_without_any_configuration() -> None:
    settings = Settings()

    assert settings.app_name == "DarwinUX"
    assert settings.env == "local"
    assert settings.log_level == "INFO"
    assert str(settings.database_url) == DEFAULT_DATABASE_URL


def test_default_database_is_local_development_and_has_no_password() -> None:
    url = Settings().database_url

    assert url.hosts()[0]["host"] == "localhost"
    assert url.path == "/darwin_dev"
    assert url.hosts()[0]["password"] is None


def test_database_url_is_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DARWIN_DATABASE_URL", "postgresql+psycopg://darwin:secret@db.internal:6543/darwin"
    )

    url = Settings().database_url

    assert url.hosts()[0]["host"] == "db.internal"
    assert url.hosts()[0]["port"] == 6543


@pytest.mark.parametrize(
    "value",
    [
        "postgresql://darwin@localhost/darwin_dev",  # would silently need psycopg2
        "mysql://darwin@localhost/darwin_dev",
        "not a url",
    ],
)
def test_database_url_must_be_postgres_with_psycopg(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DARWIN_DATABASE_URL", value)

    with pytest.raises(ValidationError):
        Settings()


def test_empty_variables_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # A .env copied from .env.example may contain `DARWIN_DATABASE_URL=`.
    monkeypatch.setenv("DARWIN_DATABASE_URL", "")
    monkeypatch.setenv("DARWIN_LOG_LEVEL", "")

    settings = Settings()

    assert str(settings.database_url) == DEFAULT_DATABASE_URL
    assert settings.log_level == "INFO"


def test_values_are_read_from_prefixed_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DARWIN_APP_NAME", "DarwinUX Dev")
    monkeypatch.setenv("DARWIN_ENV", "dev")
    monkeypatch.setenv("DARWIN_LOG_LEVEL", "DEBUG")

    settings = Settings()

    assert settings.app_name == "DarwinUX Dev"
    assert settings.env == "dev"
    assert settings.log_level == "DEBUG"


def test_unprefixed_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    assert Settings().log_level == "INFO"


def test_log_level_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARWIN_LOG_LEVEL", "warning")

    assert Settings().log_level == "WARNING"


@pytest.mark.parametrize(
    ("name", "value"),
    [("DARWIN_ENV", "production"), ("DARWIN_LOG_LEVEL", "LOUD")],
)
def test_invalid_values_fail_fast(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings()


def test_future_variables_from_env_example_do_not_break_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # .env.example already lists variables for later steps.
    monkeypatch.setenv("DARWIN_DECIDER_ADAPTER", "rules")
    monkeypatch.setenv("DARWIN_MUTATION_GENERATOR_ADAPTER", "fixture")

    Settings()
