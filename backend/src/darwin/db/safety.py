"""Guard for anything destructive against a database (tests, resets).

Integration tests create, drop, and roll back schema objects. This check makes
it impossible for them to run against anything except a local database whose
name ends in ``_test`` — so a mistyped or leaked production URL fails loudly
instead of being modified.
"""

from sqlalchemy import make_url

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
TEST_DATABASE_SUFFIX = "_test"


class UnsafeDatabaseError(RuntimeError):
    """Raised when a destructive operation targets a non-local or non-test database."""


def require_local_test_database(database_url: str) -> None:
    url = make_url(database_url)
    if url.host not in LOCAL_HOSTS:
        raise UnsafeDatabaseError(f"refusing non-local database host {url.host!r}")
    if not (url.database or "").endswith(TEST_DATABASE_SUFFIX):
        raise UnsafeDatabaseError(
            f"refusing database {url.database!r}: name must end with {TEST_DATABASE_SUFFIX!r}"
        )
