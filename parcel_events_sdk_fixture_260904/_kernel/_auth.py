"""Auth providers (doc 38 SS3.2 row 5, ``auth:`` in ``doctorine.sdk.yml``):
apiKey (header or query) / bearer / basic / oauth2 client-credentials with a
token cache, early refresh, and a configurable token endpoint. The client
calls ``authorize()`` (or ``authorize_async()``) per ATTEMPT -- fresh
material after refresh -- and ``invalidate()`` once on a 401: a
cached-but-revoked token earns exactly one transparent re-auth retry.

Vendored kernel file -- imports only sibling kernel modules (+ httpx).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Sequence

import httpx

from ._errors import APIConnectionError, APIError
from ._serialization import encode_base64
from ._transport import headers_to_dict


@dataclass(frozen=True)
class AuthMaterial:
    headers: Mapping[str, str] = field(default_factory=dict)
    query: Mapping[str, str] = field(default_factory=dict)


class AuthProvider:
    """Base provider. Static providers implement ``authorize()`` only; the
    async client transparently reuses it via ``authorize_async()``.
    Providers that perform I/O (oauth2) override both."""

    def authorize(self) -> AuthMaterial:
        raise NotImplementedError

    async def authorize_async(self) -> AuthMaterial:
        return self.authorize()

    def invalidate(self) -> bool:
        """401 hook: drop cached material. Return True when a single
        re-auth retry is worthwhile (something WAS cached and may simply
        have expired)."""
        return False


class _StaticAuth(AuthProvider):
    def __init__(self, material: AuthMaterial) -> None:
        self._material = material

    def authorize(self) -> AuthMaterial:
        return self._material


def api_key_auth(*, placement: Literal["header", "query"], name: str, key: str) -> AuthProvider:
    if placement == "header":
        return _StaticAuth(AuthMaterial(headers={name.lower(): key}))
    return _StaticAuth(AuthMaterial(query={name: key}))


def bearer_auth(token: str) -> AuthProvider:
    return _StaticAuth(AuthMaterial(headers={"authorization": f"Bearer {token}"}))


def basic_auth(*, username: str, password: str) -> AuthProvider:
    encoded = encode_base64(f"{username}:{password}")
    return _StaticAuth(AuthMaterial(headers={"authorization": f"Basic {encoded}"}))


# -- oauth2 client-credentials ------------------------------------------------


@dataclass(frozen=True)
class _CachedToken:
    access_token: str
    expires_at_ms: float


class OAuth2ClientCredentials(AuthProvider):
    """Client-credentials token cache with SINGLE-FLIGHT refresh: concurrent
    callers that miss the cache serialize on a lock and re-check it inside,
    so one fetch serves the whole burst (a cold-cache fan-out must never
    stampede the token endpoint). The sync path guards with a
    ``threading.Lock``; the async path lazily creates one ``asyncio.Lock``
    per event loop, keeping the provider event-loop-agnostic."""

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        scopes: Sequence[str] | None = None,
        transport: httpx.BaseTransport | None = None,
        async_transport: httpx.AsyncBaseTransport | None = None,
        now_ms: Callable[[], float] | None = None,
        early_refresh_ms: float = 60_000,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scopes = tuple(scopes) if scopes is not None else ()
        self._transport = transport
        self._async_transport = async_transport
        self._now_ms: Callable[[], float] = now_ms if now_ms is not None else _wall_clock_ms
        self._early_refresh_ms = early_refresh_ms
        self._token: _CachedToken | None = None
        self._sync_lock = threading.Lock()
        self._async_lock: asyncio.Lock | None = None
        self._async_lock_loop: asyncio.AbstractEventLoop | None = None

    def authorize(self) -> AuthMaterial:
        token = self._cached()
        if token is None:
            with self._sync_lock:
                token = self._cached()
                if token is None:
                    token = self._fetch()
        return AuthMaterial(headers={"authorization": f"Bearer {token.access_token}"})

    async def authorize_async(self) -> AuthMaterial:
        token = self._cached()
        if token is None:
            async with self._async_guard():
                token = self._cached()
                if token is None:
                    token = await self._fetch_async()
        return AuthMaterial(headers={"authorization": f"Bearer {token.access_token}"})

    def _fetch(self) -> _CachedToken:
        with httpx.Client(transport=self._transport) as client:
            response = client.post(self._token_url, data=self._form(), headers=_FORM_HEADERS)
            response.read()
        return self._store(response)

    async def _fetch_async(self) -> _CachedToken:
        async with httpx.AsyncClient(transport=self._async_transport) as client:
            response = await client.post(self._token_url, data=self._form(), headers=_FORM_HEADERS)
            await response.aread()
        return self._store(response)

    def _async_guard(self) -> asyncio.Lock:
        """The per-event-loop single-flight lock, created LAZILY so the
        provider never pins to a loop it was constructed outside of (an
        ``asyncio.Lock`` binds to the loop that first acquires it; a new
        loop gets a fresh lock). The check-and-swap is safe unlocked: tasks
        on one loop run single-threaded, and the exotic
        multiple-loops-in-threads case degrades to at most one fetch PER
        LOOP -- still bounded, never a per-task stampede."""
        loop = asyncio.get_running_loop()
        if self._async_lock is None or self._async_lock_loop is not loop:
            self._async_lock = asyncio.Lock()
            self._async_lock_loop = loop
        return self._async_lock

    def invalidate(self) -> bool:
        had_material = self._token is not None
        self._token = None
        return had_material

    def _cached(self) -> _CachedToken | None:
        token = self._token
        if token is not None and token.expires_at_ms - self._early_refresh_ms > self._now_ms():
            return token
        return None

    def _form(self) -> dict[str, str]:
        form = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        if self._scopes:
            form["scope"] = " ".join(self._scopes)
        return form

    def _store(self, response: httpx.Response) -> _CachedToken:
        body = _parse_json_safe(response.text)
        if response.is_error:
            raise APIError.generate(
                status=response.status_code,
                headers=headers_to_dict(response.headers),
                body=body if body is not None else response.text,
                message=f"OAuth token endpoint responded {response.status_code}",
            )
        token = self._token_from_body(body)
        self._token = token
        return token

    def _token_from_body(self, body: Any) -> _CachedToken:
        record: dict[str, Any] = body if isinstance(body, dict) else {}
        access_token = record.get("access_token")
        if not isinstance(access_token, str) or access_token == "":
            raise APIConnectionError("OAuth token endpoint returned a malformed body (no access_token)")
        expires_in = record.get("expires_in")
        expires_in_seconds = 3600.0
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) and expires_in > 0:
            expires_in_seconds = float(expires_in)
        return _CachedToken(access_token=access_token, expires_at_ms=self._now_ms() + expires_in_seconds * 1000)


_FORM_HEADERS: Mapping[str, str] = {
    "content-type": "application/x-www-form-urlencoded",
    "accept": "application/json",
}


def _wall_clock_ms() -> float:
    return time.time() * 1000


def _parse_json_safe(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None
