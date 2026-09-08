import pytest

from backend.app.core.database import _async_database_url


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("postgresql://user:pass@localhost/db", "postgresql+asyncpg://user:pass@localhost/db"),
        ("postgres://user:pass@localhost/db", "postgresql+asyncpg://user:pass@localhost/db"),
        ("postgresql+asyncpg://user:pass@localhost/db", "postgresql+asyncpg://user:pass@localhost/db"),
    ],
)
def test_application_database_url_uses_asyncpg(source: str, expected: str) -> None:
    assert _async_database_url(source) == expected


def test_non_postgresql_database_url_is_rejected() -> None:
    with pytest.raises(ValueError):
        _async_database_url("sqlite:///local.db")
