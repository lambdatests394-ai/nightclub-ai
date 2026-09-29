import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings


CURRENT = "current-internal-hmac-secret-value-0001"
PREVIOUS = "previous-internal-hmac-secret-value-002"


def settings(**values) -> Settings:
    configured = {
        "database_scheduler_url": "",
        "n8n_internal_secret": "",
        "n8n_internal_secret_previous": "",
    }
    configured.update(values)
    return Settings(_env_file=None, **configured)


def test_automation_configuration_defaults_and_optional_startup() -> None:
    configured = settings()
    assert configured.database_scheduler_url.get_secret_value() == ""
    assert configured.database_scheduler_expected_role == "nightclub_scheduler"
    assert configured.n8n_internal_secret.get_secret_value() == ""
    assert configured.automation_hmac_max_skew_seconds == 300
    assert configured.automation_publish_batch_size == 8
    assert configured.automation_publish_max_concurrency == 4


@pytest.mark.parametrize("role", ["Nightclub_scheduler", "nightclub-scheduler", "9scheduler", "x" * 64])
def test_scheduler_role_identifier_is_restricted(role: str) -> None:
    with pytest.raises(ValidationError):
        settings(database_scheduler_expected_role=role)


@pytest.mark.parametrize("value", [" " + CURRENT, CURRENT + " ", "line\nbreak" + "x" * 30, "x" * 31])
def test_internal_secret_rejects_noncanonical_or_short_values(value: str) -> None:
    with pytest.raises(ValidationError) as error:
        settings(n8n_internal_secret=value)
    assert value not in str(error.value)


def test_rotation_secrets_must_be_distinct_and_primary_present() -> None:
    with pytest.raises(ValidationError):
        settings(n8n_internal_secret=CURRENT, n8n_internal_secret_previous=CURRENT)
    with pytest.raises(ValidationError):
        settings(n8n_internal_secret_previous=PREVIOUS)
    configured = settings(n8n_internal_secret=CURRENT, n8n_internal_secret_previous=PREVIOUS)
    assert configured.n8n_internal_secret_previous.get_secret_value() == PREVIOUS


@pytest.mark.parametrize("field,invalid", [
    ("automation_hmac_max_skew_seconds", 29),
    ("automation_hmac_max_skew_seconds", 601),
    ("automation_publish_batch_size", 0),
    ("automation_publish_batch_size", 33),
    ("automation_publish_max_concurrency", 0),
    ("automation_publish_max_concurrency", 9),
])
def test_automation_numeric_bounds(field: str, invalid: int) -> None:
    with pytest.raises(ValidationError):
        settings(**{field: invalid})


def test_concurrency_cannot_exceed_batch() -> None:
    with pytest.raises(ValidationError):
        settings(automation_publish_batch_size=2, automation_publish_max_concurrency=3)


def test_scheduler_url_and_secrets_are_not_rendered_or_serialized() -> None:
    configured = settings(
        database_scheduler_url="postgresql://scheduler:synthetic@localhost/db",
        n8n_internal_secret=CURRENT,
        n8n_internal_secret_previous=PREVIOUS,
    )
    rendered = repr(configured)
    dumped = configured.model_dump()
    for secret in ("synthetic", CURRENT, PREVIOUS):
        assert secret not in rendered
        assert secret not in str(dumped)
    assert "database_scheduler_url" not in dumped
    assert "n8n_internal_secret" not in dumped
    assert "n8n_internal_secret_previous" not in dumped
