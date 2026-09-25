import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr

from backend.app.modules.integrations.errors import (
    FacebookInvalidOAuthState,
    FacebookNotConfigured,
    FacebookOAuthStateExpired,
)
from backend.app.modules.integrations.oauth_state import (
    OAuthStateCodec,
    STATE_VERSION,
    nonce_digest,
    state_digest,
)


class Clock:
    def __init__(self):
        self.now = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.now


def key():
    return base64.urlsafe_b64encode(bytes(range(31, -1, -1))).decode().rstrip("=")


@pytest.fixture
def material():
    clock = Clock()
    return clock, OAuthStateCodec(SecretStr(key()), clock=clock)


def test_state_round_trip_is_canonical_versioned_and_digestible(material):
    clock, codec = material
    organization_id, actor_id = uuid4(), uuid4()
    issued = codec.issue(organization_id, actor_id, "123456")
    token = issued.reveal_token()
    restored = codec.verify(token)
    payload_segment = token.split(".")[0]
    payload = base64.urlsafe_b64decode(payload_segment + "=" * (-len(payload_segment) % 4))
    parsed = json.loads(payload)
    assert payload == json.dumps(parsed, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=True, allow_nan=False).encode()
    assert restored.organization_id == organization_id
    assert restored.actor_id == actor_id
    assert restored.requested_page_id == "123456"
    assert restored.version == STATE_VERSION == 1
    assert restored.expires_at - restored.issued_at == timedelta(minutes=10)
    assert state_digest(token) == issued.state_digest
    assert nonce_digest(restored.nonce.get_secret_value()) == issued.nonce_digest


@pytest.mark.parametrize("mutation", ["payload", "mac", "malformed"])
def test_tampering_or_malformed_state_is_rejected(material, mutation):
    _, codec = material
    token = codec.issue(uuid4(), uuid4(), "123").reveal_token()
    payload, mac = token.split(".")
    if mutation == "payload":
        token = ("A" if payload[0] != "A" else "B") + payload[1:] + "." + mac
    elif mutation == "mac":
        token = payload + "." + ("A" if mac[0] != "A" else "B") + mac[1:]
    else:
        token = "not-a-state"
    with pytest.raises(FacebookInvalidOAuthState):
        codec.verify(token)


def test_expired_state_uses_fixed_clock_without_sleep(material):
    clock, codec = material
    token = codec.issue(uuid4(), uuid4(), "123").reveal_token()
    clock.now += timedelta(minutes=10)
    with pytest.raises(FacebookOAuthStateExpired):
        codec.verify(token)


def test_future_issued_at_is_rejected():
    issuer_clock, verifier_clock = Clock(), Clock()
    issuer_clock.now += timedelta(minutes=2)
    token = OAuthStateCodec(key(), clock=issuer_clock).issue(uuid4(), uuid4(), "123").reveal_token()
    with pytest.raises(FacebookInvalidOAuthState):
        OAuthStateCodec(key(), clock=verifier_clock).verify(token)


@pytest.mark.parametrize("bad", ["", "plain-text-passphrase", base64.urlsafe_b64encode(b"short").decode().rstrip("=")])
def test_state_key_configuration_fails_closed_and_never_uses_app_secret(bad):
    with pytest.raises(FacebookNotConfigured):
        OAuthStateCodec(bad)


def test_state_objects_hide_token_and_nonce(material):
    _, codec = material
    issued = codec.issue(uuid4(), uuid4(), "123")
    assert issued.reveal_token() not in repr(issued)
    assert issued.state.nonce.get_secret_value() not in repr(issued.state)
    assert key() not in repr(codec)


def test_idempotent_issue_reconstructs_same_token_without_storing_it(material):
    clock, codec = material
    organization_id, actor_id, idempotency_key = uuid4(), uuid4(), uuid4()
    first = codec.issue_idempotent(
        organization_id, actor_id, "123", idempotency_key, issued_at=clock.now,
    )
    second = codec.issue_idempotent(
        organization_id, actor_id, "123", idempotency_key, issued_at=clock.now,
    )
    assert first.reveal_token() == second.reveal_token()
    assert first.state_digest == second.state_digest
