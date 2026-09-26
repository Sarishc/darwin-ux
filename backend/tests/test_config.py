import pytest
from pydantic import ValidationError

from darwin.config import Settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Make every test start from an empty DARWIN_* environment.
    for name in ("DARWIN_APP_NAME", "DARWIN_ENV", "DARWIN_LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_allow_starting_without_any_configuration() -> None:
    settings = Settings()

    assert settings.app_name == "DarwinUX"
    assert settings.env == "local"
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
    monkeypatch.setenv("DARWIN_DATABASE_URL", "")
    monkeypatch.setenv("DARWIN_DECIDER_ADAPTER", "rules")

    Settings()
