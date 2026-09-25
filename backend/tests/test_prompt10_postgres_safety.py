"""Regression tests for exact Prompt 10 disposable-database guards."""

import pytest
from sqlalchemy.engine import URL

from backend.tests.prompt10_postgres_safety import (
    PROMPT10_CERT_DATABASES,
    validated_prompt10_pair,
    validated_prompt10_url,
)


def url(database, *, username="nightclub_api", host="127.0.0.1",
        driver="postgresql+asyncpg"):
    return URL.create(
        driver, username=username, password="synthetic-local-only",
        host=host, port=5432, database=database,
    )


@pytest.mark.parametrize("database", sorted(PROMPT10_CERT_DATABASES))
def test_exact_prompt10_certification_databases_are_accepted(database):
    runtime = validated_prompt10_url(url(database), "nightclub_api")
    migration, paired_runtime = validated_prompt10_pair(
        url(database, username="alembic_test_user", driver="postgresql+psycopg"),
        url(database),
    )
    assert runtime.database == migration.database == paired_runtime.database == database


@pytest.mark.parametrize("database", [
    "nightclub_ai", "postgres", "template1", "production", "random_database",
])
def test_non_allowlisted_database_is_rejected(database):
    with pytest.raises(ValueError):
        validated_prompt10_url(url(database), "nightclub_api")


@pytest.mark.parametrize("unsafe", [
    url("nightclub_ai_prompt10_b2e_cert_test", host="database.example.test"),
    url("nightclub_ai_prompt10_b2e_cert_test", username="postgres"),
    url("nightclub_ai_prompt10_b2e_cert_test", driver="postgresql"),
])
def test_remote_host_wrong_role_and_unapproved_driver_are_rejected(unsafe):
    with pytest.raises(ValueError):
        validated_prompt10_url(unsafe, "nightclub_api")


def test_migration_and_runtime_must_target_same_certification_database():
    with pytest.raises(ValueError):
        validated_prompt10_pair(
            url(
                "nightclub_ai_prompt10_b2c_cert_test",
                username="alembic_test_user", driver="postgresql+psycopg",
            ),
            url("nightclub_ai_prompt10_b2e_cert_test"),
        )
