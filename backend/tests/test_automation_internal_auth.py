import hashlib
import hmac

import pytest
from pydantic import SecretStr

from backend.app.modules.automation.internal_auth import (
    InternalAuthenticationFailed,
    InternalHMACAuthenticator,
    canonical_payload,
)


NOW = 1_760_000_000
CURRENT = "current-internal-hmac-secret-value-0001"
PREVIOUS = "previous-internal-hmac-secret-value-002"
PATH = "/internal/automation/publish-due"


def signature(secret: str, *, timestamp: str = str(NOW), method: str = "POST",
              path: str = PATH, body: bytes = b"") -> str:
    payload = canonical_payload(method=method, path=path, timestamp=timestamp, raw_body=body)
    return "v1=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def authenticator(*, current: str = CURRENT, previous: str = "", skew: int = 300):
    return InternalHMACAuthenticator(
        SecretStr(current), SecretStr(previous), max_skew_seconds=skew, clock=lambda: NOW,
    )


def authenticate(auth=None, *, timestamp: str = str(NOW), signature_value: str | None = None,
                 method: str = "POST", path: str = PATH, body: bytes = b"",
                 timestamps=None, signatures=None) -> None:
    auth = auth or authenticator()
    auth.authenticate(
        method=method,
        path=path,
        raw_body=body,
        timestamp_values=[timestamp] if timestamps is None else timestamps,
        signature_values=[signature_value or signature(CURRENT, timestamp=timestamp)]
        if signatures is None else signatures,
    )


def test_frozen_canonical_payload_has_lf_and_no_trailing_newline() -> None:
    payload = canonical_payload(method="POST", path=PATH, timestamp=str(NOW), raw_body=b"")
    expected_digest = hashlib.sha256(b"").hexdigest()
    assert payload == f"v1\nPOST\n{PATH}\n{NOW}\n{expected_digest}".encode("utf-8")
    assert not payload.endswith(b"\n")


@pytest.mark.parametrize("secret", [CURRENT, PREVIOUS])
def test_current_and_previous_rotation_secrets_validate(secret: str) -> None:
    authenticate(
        authenticator(previous=PREVIOUS),
        signature_value=signature(secret),
    )


def test_rotation_performs_both_constant_time_comparisons(monkeypatch) -> None:
    comparisons = []
    original = hmac.compare_digest

    def record(expected, supplied):
        comparisons.append((expected, supplied))
        return original(expected, supplied)

    monkeypatch.setattr(
        "backend.app.modules.automation.internal_auth.hmac.compare_digest", record,
    )
    authenticate(authenticator(previous=PREVIOUS), signature_value=signature(CURRENT))
    assert len(comparisons) == 2


@pytest.mark.parametrize("timestamp", [str(NOW - 300), str(NOW + 300)])
def test_exact_timestamp_boundaries_are_accepted(timestamp: str) -> None:
    authenticate(timestamp=timestamp, signature_value=signature(CURRENT, timestamp=timestamp))


@pytest.mark.parametrize("timestamp", ["", "+1760000000", "01760000000", "1760000000.0",
                                          "1e9", " 1760000000", "1760000000 ", "-1"])
def test_malformed_timestamp_has_generic_failure(timestamp: str) -> None:
    with pytest.raises(InternalAuthenticationFailed):
        authenticate(timestamp=timestamp, signature_value="v1=" + "0" * 64)


@pytest.mark.parametrize("timestamp", [str(NOW - 301), str(NOW + 301)])
def test_stale_or_future_timestamp_has_generic_failure(timestamp: str) -> None:
    with pytest.raises(InternalAuthenticationFailed):
        authenticate(timestamp=timestamp, signature_value=signature(CURRENT, timestamp=timestamp))


@pytest.mark.parametrize("timestamps,signatures", [
    ([], ["v1=" + "0" * 64]),
    ([str(NOW), str(NOW)], ["v1=" + "0" * 64]),
    ([str(NOW)], []),
    ([str(NOW)], ["v1=" + "0" * 64, "v1=" + "0" * 64]),
])
def test_missing_or_duplicate_headers_have_generic_failure(timestamps, signatures) -> None:
    with pytest.raises(InternalAuthenticationFailed):
        authenticate(timestamps=timestamps, signatures=signatures)


@pytest.mark.parametrize("timestamps,signatures", [
    (None, ["v1=" + "0" * 64]),
    ([None], ["v1=" + "0" * 64]),
    ([str(NOW)], [None]),
])
def test_invalid_header_types_have_generic_failure(timestamps, signatures) -> None:
    with pytest.raises(InternalAuthenticationFailed):
        authenticate(timestamps=timestamps, signatures=signatures)


@pytest.mark.parametrize("bad_signature", [
    "" , "0" * 64, "v2=" + "0" * 64, "v1=" + "A" * 64,
    "v1=" + "g" * 64, "v1=" + "0" * 63, " v1=" + "0" * 64,
    "v1=" + "0" * 64 + ",v1=" + "1" * 64,
])
def test_noncanonical_signature_has_generic_failure(bad_signature: str) -> None:
    with pytest.raises(InternalAuthenticationFailed):
        authenticate(signatures=[bad_signature])


@pytest.mark.parametrize("change", ["secret", "method", "path", "body"])
def test_signature_is_bound_to_all_request_material(change: str) -> None:
    kwargs = {"signature_value": signature(CURRENT)}
    if change == "secret": kwargs["signature_value"] = signature("wrong-secret-value-with-enough-bytes")
    if change == "method": kwargs["method"] = "GET"
    if change == "path": kwargs["path"] = "/internal/automation/other"
    if change == "body": kwargs["body"] = b"{}"
    with pytest.raises(InternalAuthenticationFailed):
        authenticate(**kwargs)


def test_absent_server_secret_has_generic_nonleaking_failure() -> None:
    supplied = signature(CURRENT)
    with pytest.raises(InternalAuthenticationFailed) as error:
        authenticate(authenticator(current=""), signature_value=supplied)
    message = str(error.value)
    assert message == "Internal authentication failed"
    assert CURRENT not in message and supplied not in message and PATH not in message
