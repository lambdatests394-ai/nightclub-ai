"""Narrow, bounded Meta Graph publication boundary for Facebook Page posts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import json
import re
from typing import Protocol
from urllib.parse import quote, urlencode, urlsplit

import httpx
from pydantic import SecretStr

from backend.app.modules.integrations.errors import (
    FacebookInvalidConnection,
    FacebookNotConfigured,
    FacebookProviderUnavailable,
)
from backend.app.modules.integrations.schemas import (
    FacebookCapabilities,
    VerifiedFacebookPage,
)
from backend.app.modules.integrations.scopes import FACEBOOK_OAUTH_SCOPES


GRAPH_ORIGIN = "https://graph.facebook.com"
MAX_RESPONSE_BYTES = 65_536
MAX_EXTERNAL_ID_LENGTH = 512
MAX_REQUEST_ID_LENGTH = 255
RECONCILIATION_CANDIDATE_LIMIT = 25
FACEBOOK_AUTH_ORIGIN = "https://www.facebook.com"
MAX_OAUTH_CODE_LENGTH = 2048
MAX_ACCESS_TOKEN_LENGTH = 8192
MAX_PAGE_CANDIDATES = 100


class ProviderDisposition(StrEnum):
    SUCCEEDED = "succeeded"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"
    AMBIGUOUS = "ambiguous"


class ReconciliationDisposition(StrEnum):
    MATCHED = "matched"
    NOT_FOUND = "not_found"
    AMBIGUOUS_MULTIPLE = "ambiguous_multiple"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class ProviderResult:
    disposition: ProviderDisposition
    external_post_id: str | None = None
    provider_request_id: str | None = None
    http_status: int | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    disposition: ReconciliationDisposition
    external_post_id: str | None = None
    provider_request_id: str | None = None
    http_status: int | None = None
    error_code: str | None = None


class FacebookPublicationProvider(Protocol):
    async def publish_post(
        self, *, page_id: str, message: str, link_url: str | None,
        access_token: SecretStr,
    ) -> ProviderResult: ...

    async def reconcile_post(
        self, *, page_id: str, message: str, link_url: str | None,
        attempt_started_at: datetime, observed_at: datetime,
        access_token: SecretStr,
    ) -> ReconciliationResult: ...


class FacebookOAuthProvider(Protocol):
    def authorization_url(self, state: str) -> str: ...

    async def exchange_page(
        self, *, authorization_code: SecretStr, requested_page_id: str,
    ) -> VerifiedFacebookPage: ...


def _bounded_text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (not normalized or len(normalized) > limit
            or any(ord(character) < 32 for character in normalized)):
        return None
    return normalized


def _bounded_exact(value, limit: int) -> str | None:
    if (not isinstance(value, str) or not value or len(value) > limit
            or value != value.strip()
            or any(ord(character) < 32 for character in value)):
        return None
    return value


def _request_id(response: httpx.Response, payload: object = None) -> str | None:
    error = payload.get("error") if isinstance(payload, dict) else None
    candidates = (
        response.headers.get("x-fb-trace-id"),
        response.headers.get("x-fb-request-id"),
        payload.get("fbtrace_id") if isinstance(payload, dict) else None,
        error.get("fbtrace_id") if isinstance(error, dict) else None,
    )
    for candidate in candidates:
        normalized = _bounded_text(candidate, MAX_REQUEST_ID_LENGTH)
        if normalized is not None:
            return normalized
    return None


def _safe_json(response: httpx.Response) -> object | None:
    if response.extensions.get("nightclub_response_oversized"):
        return None
    content = response.content
    if len(content) > MAX_RESPONSE_BYTES:
        return None
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None


def _safe_error(payload: object) -> tuple[int | None, int | None, bool, str | None]:
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None, None, False, None
    code = error.get("code") if isinstance(error.get("code"), int) else None
    subcode = error.get("error_subcode") if isinstance(error.get("error_subcode"), int) else None
    transient = error.get("is_transient") is True
    trace = _bounded_text(error.get("fbtrace_id"), MAX_REQUEST_ID_LENGTH)
    return code, subcode, transient, trace


class MetaGraphPublicationAdapter:
    """One HTTP operation per method; the adapter never retries POST requests."""

    def __init__(self, graph_version: str, *, client: httpx.AsyncClient | None = None):
        if (not isinstance(graph_version, str)
                or re.fullmatch(r"v[1-9][0-9]*\.[0-9]+", graph_version) is None):
            raise ValueError("A valid server-owned Meta Graph version is required")
        self.graph_version = graph_version
        self._client = client

    def __repr__(self) -> str:
        return f"MetaGraphPublicationAdapter(graph_version={self.graph_version!r})"

    def _url(self, page_id: str, resource: str = "feed") -> str:
        page = quote(page_id, safe="")
        return f"{GRAPH_ORIGIN}/{self.graph_version}/{page}/{resource}"

    async def _request(self, method: str, url: str, *, token: SecretStr, **kwargs):
        headers = {"Authorization": f"Bearer {token.get_secret_value()}"}
        owned = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0), follow_redirects=False,
        )
        response = None
        try:
            request = client.build_request(method, url, headers=headers, **kwargs)
            async with asyncio.timeout(15):
                response = await client.send(request, stream=True)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        await response.aclose()
                        return httpx.Response(
                            response.status_code,
                            headers=response.headers,
                            content=b"",
                            request=request,
                            extensions={"nightclub_response_oversized": True},
                        )
                    body.extend(chunk)
                await response.aclose()
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=bytes(body),
                    request=request,
                )
        finally:
            if response is not None:
                await response.aclose()
            if owned:
                await client.aclose()

    async def publish_post(
        self, *, page_id: str, message: str, link_url: str | None,
        access_token: SecretStr,
    ) -> ProviderResult:
        form = {"message": message}
        if link_url is not None:
            form["link"] = link_url
        try:
            response = await self._request(
                "POST", self._url(page_id), token=access_token, data=form,
            )
        except (TimeoutError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError,
                httpx.WriteError, httpx.RemoteProtocolError):
            return ProviderResult(
                ProviderDisposition.AMBIGUOUS,
                error_code="META_PUBLICATION_AMBIGUOUS",
                error_message="Publication result was ambiguous",
            )
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout):
            return ProviderResult(
                ProviderDisposition.RETRYABLE,
                error_code="META_CONNECTION_UNAVAILABLE",
                error_message="Provider connection was unavailable",
            )

        payload = _safe_json(response)
        request_id = _request_id(response, payload)
        if response.is_success:
            external_id = _bounded_text(
                payload.get("id") if isinstance(payload, dict) else None,
                MAX_EXTERNAL_ID_LENGTH,
            )
            if external_id is None:
                return ProviderResult(
                    ProviderDisposition.AMBIGUOUS,
                    provider_request_id=request_id,
                    http_status=response.status_code,
                    error_code="META_MALFORMED_SUCCESS",
                    error_message="Provider response was malformed",
                )
            return ProviderResult(
                ProviderDisposition.SUCCEEDED,
                external_post_id=external_id,
                provider_request_id=request_id,
                http_status=response.status_code,
            )

        code, subcode, transient, trace = _safe_error(payload)
        request_id = request_id or trace
        normalized = f"META_{code}" if code is not None else "META_HTTP_ERROR"
        if subcode is not None:
            normalized += f"_{subcode}"
        if response.status_code == 429 or transient or code in {4, 17, 32, 613}:
            return ProviderResult(
                ProviderDisposition.RETRYABLE,
                provider_request_id=request_id,
                http_status=response.status_code,
                error_code=normalized,
                error_message="Provider rate limited publication",
            )
        if response.status_code >= 500:
            return ProviderResult(
                ProviderDisposition.RETRYABLE,
                provider_request_id=request_id,
                http_status=response.status_code,
                error_code=normalized,
                error_message="Provider temporarily rejected publication",
            )
        authentication = response.status_code in {401, 403} or code in {10, 102, 190, 200}
        return ProviderResult(
            ProviderDisposition.PERMANENT,
            provider_request_id=request_id,
            http_status=response.status_code,
            error_code="META_AUTHENTICATION_FAILED" if authentication else normalized,
            error_message=(
                "Provider authentication failed" if authentication
                else "Provider rejected publication"
            ),
        )

    async def reconcile_post(
        self, *, page_id: str, message: str, link_url: str | None,
        attempt_started_at: datetime, observed_at: datetime,
        access_token: SecretStr,
    ) -> ReconciliationResult:
        since = int(attempt_started_at.timestamp()) - 300
        try:
            response = await self._request(
                "GET", self._url(page_id), token=access_token,
                params={
                    "fields": "id,message,created_time,link",
                    "limit": str(RECONCILIATION_CANDIDATE_LIMIT),
                    "since": str(since),
                },
            )
        except (TimeoutError, httpx.HTTPError):
            return ReconciliationResult(
                ReconciliationDisposition.INCONCLUSIVE,
                error_code="META_RECONCILIATION_UNAVAILABLE",
            )
        payload = _safe_json(response)
        request_id = _request_id(response, payload)
        if not response.is_success or not isinstance(payload, dict):
            return ReconciliationResult(
                ReconciliationDisposition.INCONCLUSIVE,
                provider_request_id=request_id,
                http_status=response.status_code,
                error_code="META_RECONCILIATION_RESPONSE_INVALID",
            )
        candidates = payload.get("data")
        if not isinstance(candidates, list) or len(candidates) > RECONCILIATION_CANDIDATE_LIMIT:
            return ReconciliationResult(
                ReconciliationDisposition.INCONCLUSIVE,
                provider_request_id=request_id,
                http_status=response.status_code,
                error_code="META_RECONCILIATION_RESPONSE_INVALID",
            )
        matches: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                return ReconciliationResult(
                    ReconciliationDisposition.INCONCLUSIVE,
                    provider_request_id=request_id, http_status=response.status_code,
                    error_code="META_RECONCILIATION_FIELDS_MISSING",
                )
            external_id = _bounded_text(candidate.get("id"), MAX_EXTERNAL_ID_LENGTH)
            candidate_message = candidate.get("message")
            created_raw = candidate.get("created_time")
            if external_id is None or not isinstance(candidate_message, str) or not isinstance(created_raw, str):
                return ReconciliationResult(
                    ReconciliationDisposition.INCONCLUSIVE,
                    provider_request_id=request_id, http_status=response.status_code,
                    error_code="META_RECONCILIATION_FIELDS_MISSING",
                )
            try:
                created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except ValueError:
                return ReconciliationResult(
                    ReconciliationDisposition.INCONCLUSIVE,
                    provider_request_id=request_id, http_status=response.status_code,
                    error_code="META_RECONCILIATION_FIELDS_INVALID",
                )
            if created.tzinfo is None or created.utcoffset() is None:
                return ReconciliationResult(
                    ReconciliationDisposition.INCONCLUSIVE,
                    provider_request_id=request_id, http_status=response.status_code,
                    error_code="META_RECONCILIATION_FIELDS_INVALID",
                )
            if not (attempt_started_at.timestamp() - 300 <= created.timestamp() <= observed_at.timestamp()):
                continue
            if candidate_message != message:
                continue
            candidate_link = candidate.get("link")
            if link_url is None:
                if candidate_link not in {None, ""}:
                    continue
            elif not isinstance(candidate_link, str):
                return ReconciliationResult(
                    ReconciliationDisposition.INCONCLUSIVE,
                    provider_request_id=request_id, http_status=response.status_code,
                    error_code="META_RECONCILIATION_FIELDS_MISSING",
                )
            elif candidate_link != link_url:
                continue
            matches.append(external_id)
        if len(matches) == 1:
            return ReconciliationResult(
                ReconciliationDisposition.MATCHED,
                external_post_id=matches[0], provider_request_id=request_id,
                http_status=response.status_code,
            )
        if len(matches) > 1:
            return ReconciliationResult(
                ReconciliationDisposition.AMBIGUOUS_MULTIPLE,
                provider_request_id=request_id, http_status=response.status_code,
                error_code="RECONCILIATION_MULTIPLE_MATCHES",
            )
        return ReconciliationResult(
            ReconciliationDisposition.NOT_FOUND,
            provider_request_id=request_id, http_status=response.status_code,
        )


class MetaGraphOAuthAdapter:
    """Server-owned Meta OAuth/Page discovery boundary with bounded responses."""

    def __init__(
            self, *, graph_version: str, app_id: str, app_secret: SecretStr,
            redirect_uri: str, client: httpx.AsyncClient | None = None):
        redirect = urlsplit(redirect_uri)
        if (re.fullmatch(r"v[1-9][0-9]*\.[0-9]+", graph_version or "") is None
                or re.fullmatch(r"[1-9][0-9]{0,63}", app_id or "") is None
                or not app_secret.get_secret_value()
                or redirect.scheme != "https" or not redirect.netloc
                or redirect.username is not None or redirect.password is not None
                or redirect.fragment):
            raise FacebookNotConfigured()
        self.graph_version = graph_version
        self.app_id = app_id
        self._app_secret = app_secret
        self.redirect_uri = redirect_uri
        self._client = client

    def __repr__(self) -> str:
        return (
            "MetaGraphOAuthAdapter("
            f"graph_version={self.graph_version!r}, app_id={self.app_id!r}, "
            "app_secret=**********, redirect_uri=[configured])"
        )

    def authorization_url(self, state: str) -> str:
        if _bounded_exact(state, 4096) is None:
            raise FacebookInvalidConnection()
        query = urlencode({
            "client_id": self.app_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": ",".join(FACEBOOK_OAUTH_SCOPES),
            "state": state,
        })
        return f"{FACEBOOK_AUTH_ORIGIN}/{self.graph_version}/dialog/oauth?{query}"

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        owned = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0), follow_redirects=False, trust_env=False,
        )
        response = None
        try:
            request = client.build_request(method, url, **kwargs)
            async with asyncio.timeout(15):
                response = await client.send(request, stream=True)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise FacebookProviderUnavailable()
                    body.extend(chunk)
                return httpx.Response(
                    response.status_code, headers=response.headers,
                    content=bytes(body), request=request,
                )
        except FacebookProviderUnavailable:
            raise
        except (TimeoutError, httpx.HTTPError):
            raise FacebookProviderUnavailable() from None
        finally:
            if response is not None:
                await response.aclose()
            if owned:
                await client.aclose()

    @staticmethod
    def _object(response: httpx.Response) -> dict:
        payload = _safe_json(response)
        if not response.is_success or not isinstance(payload, dict):
            raise FacebookProviderUnavailable()
        return payload

    async def exchange_page(
            self, *, authorization_code: SecretStr,
            requested_page_id: str) -> VerifiedFacebookPage:
        code = _bounded_exact(
            authorization_code.get_secret_value(), MAX_OAUTH_CODE_LENGTH,
        )
        if code is None or _bounded_text(requested_page_id, 128) is None:
            raise FacebookInvalidConnection()
        token_response = await self._request(
            "POST", f"{GRAPH_ORIGIN}/{self.graph_version}/oauth/access_token",
            data={
                "client_id": self.app_id,
                "client_secret": self._app_secret.get_secret_value(),
                "redirect_uri": self.redirect_uri,
                "code": code,
            },
        )
        token_payload = self._object(token_response)
        user_token = _bounded_exact(
            token_payload.get("access_token"), MAX_ACCESS_TOKEN_LENGTH,
        )
        token_type = _bounded_text(token_payload.get("token_type"), 32)
        if user_token is None:
            raise FacebookProviderUnavailable()

        accounts_response = await self._request(
            "GET", f"{GRAPH_ORIGIN}/{self.graph_version}/me/accounts",
            headers={"Authorization": f"Bearer {user_token}"},
            params={
                "fields": "id,name,access_token,tasks",
                "limit": str(MAX_PAGE_CANDIDATES),
            },
        )
        accounts = self._object(accounts_response).get("data")
        if not isinstance(accounts, list) or len(accounts) > MAX_PAGE_CANDIDATES:
            raise FacebookProviderUnavailable()
        matches = [
            page for page in accounts
            if isinstance(page, dict) and page.get("id") == requested_page_id
        ]
        if len(matches) != 1:
            raise FacebookInvalidConnection()
        page = matches[0]
        page_token = _bounded_exact(page.get("access_token"), MAX_ACCESS_TOKEN_LENGTH)
        display_name = _bounded_text(page.get("name"), 255)
        tasks = page.get("tasks")
        if (page_token is None or display_name is None or not isinstance(tasks, list)
                or not all(isinstance(task, str) for task in tasks)
                or "CREATE_CONTENT" not in tasks):
            raise FacebookInvalidConnection()
        return VerifiedFacebookPage(
            external_account_id=requested_page_id,
            display_name=display_name,
            capabilities=FacebookCapabilities(
                can_publish_posts=True,
                can_read_engagement=True,
                can_list_pages=True,
            ),
            access_token=SecretStr(page_token),
            token_type=token_type,
            token_expires_at=None,
        )
