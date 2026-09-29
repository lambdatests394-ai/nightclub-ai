"""Reusable HMAC authentication primitives for internal automation triggers."""

from collections.abc import Callable, Sequence
import hashlib
import hmac
import re
import time

from pydantic import SecretStr


_TIMESTAMP = re.compile(r"(?:0|[1-9][0-9]*)", re.ASCII)
_SIGNATURE = re.compile(r"v1=[0-9a-f]{64}", re.ASCII)


class InternalAuthenticationFailed(Exception):
    """Generic failure that intentionally reveals no authentication detail."""

    def __init__(self) -> None:
        super().__init__("Internal authentication failed")


def canonical_payload(*, method: str, path: str, timestamp: str, raw_body: bytes) -> bytes:
    """Build the frozen v1 representation from exact request components."""

    body_digest = hashlib.sha256(raw_body).hexdigest()
    return f"v1\n{method}\n{path}\n{timestamp}\n{body_digest}".encode("utf-8")


class InternalHMACAuthenticator:
    """Validate one timestamp/signature pair against current rotation secrets."""

    def __init__(
        self,
        current_secret: SecretStr,
        previous_secret: SecretStr | None = None,
        *,
        max_skew_seconds: int = 300,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._current_secret = current_secret
        self._previous_secret = previous_secret or SecretStr("")
        self._max_skew_seconds = max_skew_seconds
        self._clock = clock

    def authenticate(
        self,
        *,
        method: str,
        path: str,
        raw_body: bytes,
        timestamp_values: Sequence[str],
        signature_values: Sequence[str],
    ) -> None:
        """Authenticate exact raw request material or raise one generic failure."""

        if (not isinstance(timestamp_values, Sequence)
                or not isinstance(signature_values, Sequence)
                or isinstance(timestamp_values, str) or isinstance(signature_values, str)
                or len(timestamp_values) != 1 or len(signature_values) != 1):
            raise InternalAuthenticationFailed()
        timestamp = timestamp_values[0]
        supplied_signature = signature_values[0]
        if (not isinstance(timestamp, str) or not isinstance(supplied_signature, str)
                or _TIMESTAMP.fullmatch(timestamp) is None
                or _SIGNATURE.fullmatch(supplied_signature) is None):
            raise InternalAuthenticationFailed()
        if (not isinstance(method, str) or not method
                or not isinstance(path, str) or not path.startswith("/")
                or "?" in path or "#" in path
                or any(ch in method or ch in path for ch in ("\r", "\n"))
                or not isinstance(raw_body, bytes)):
            raise InternalAuthenticationFailed()

        try:
            request_time = int(timestamp)
        except (ValueError, TypeError):
            raise InternalAuthenticationFailed() from None
        if abs(self._clock() - request_time) > self._max_skew_seconds:
            raise InternalAuthenticationFailed()

        current = self._current_secret.get_secret_value()
        previous = self._previous_secret.get_secret_value()
        if not current:
            raise InternalAuthenticationFailed()

        payload = canonical_payload(
            method=method, path=path, timestamp=timestamp, raw_body=raw_body,
        )
        current_signature = "v1=" + hmac.new(
            current.encode("utf-8"), payload, hashlib.sha256,
        ).hexdigest()
        current_matches = hmac.compare_digest(current_signature, supplied_signature)

        # When rotation is enabled, always perform both constant-time comparisons.
        previous_matches = False
        if previous:
            previous_signature = "v1=" + hmac.new(
                previous.encode("utf-8"), payload, hashlib.sha256,
            ).hexdigest()
            previous_matches = hmac.compare_digest(previous_signature, supplied_signature)

        if not (current_matches or previous_matches):
            raise InternalAuthenticationFailed()
