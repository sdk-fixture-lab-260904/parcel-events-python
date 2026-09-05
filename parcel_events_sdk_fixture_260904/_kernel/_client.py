"""The core sync client (doc 38 SS1 "Emitters + kernels" row): the
httpx-based transport orchestrator every generated resource method calls
into. One request = prepare (URL, headers, JSON body, ONE idempotency key)
-> the retry loop (auth per attempt / per-attempt timeout / backoff with
Retry-After / one transparent re-auth on 401) -> typed error or parsed body.
Also the `SyncPageFetcher` pagination iterates through, and the raw response
source streaming reads from. The shared request-preparation core
(`ClientCore`) is reused verbatim by `_client_async.py`.

Vendored kernel file -- imports only sibling kernel modules (+ httpx).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Mapping, Sequence, TypeVar, Union

import httpx

from ._auth import AuthMaterial, AuthProvider
from ._errors import APIConnectionError, APIError
from ._idempotency import IdempotencyConfig, resolve_idempotency_key
from ._pagination_locators import PageRequest
from ._retries import (
    RetryDecision,
    RetryPolicy,
    next_retry_decision,
    parse_retry_after_ms,
    resolve_retry_policy,
)
from ._serialization import (
    HttpxFileEntry,
    MultipartFieldValue,
    QueryParamValue,
    QueryStyle,
    append_query,
    encode_query,
    json_body,
)
from ._transport import (
    DEFAULT_TIMEOUT_MS,
    headers_to_dict,
    map_transport_error,
    parse_response_body,
    request_timeout,
)
from ._version import user_agent

T = TypeVar("T")

MultipartFields = Mapping[str, Union[MultipartFieldValue, Sequence[MultipartFieldValue]]]


@dataclass(frozen=True)
class APIResponse(Generic[T]):
    status: int
    headers: Mapping[str, str]
    data: T


@dataclass(frozen=True)
class PreparedRequest:
    """One logical request, fully resolved BEFORE the retry loop (so the
    idempotency key is stable across every retry of this call)."""

    method: str
    url: str
    headers: dict[str, str]
    content: "bytes | str | None"
    data: list[tuple[str, str]] | None
    files: list[HttpxFileEntry] | None
    timeout_ms: float


def _lowercase_keys(record: Mapping[str, str] | None) -> dict[str, str]:
    if record is None:
        return {}
    return {key.lower(): value for key, value in record.items()}


def _wall_clock_ms() -> float:
    return time.time() * 1000


@dataclass(frozen=True)
class ClientCore:
    """The transport-agnostic half shared by SyncClient and AsyncClient:
    request preparation and retry decisions -- everything except the I/O."""

    base_url: str
    user_agent_value: str
    timeout_ms: float
    retry_policy: RetryPolicy
    default_headers: Mapping[str, str]
    idempotency: IdempotencyConfig
    now_ms: Callable[[], float] = field(default=_wall_clock_ms)

    @staticmethod
    def create(
        *,
        base_url: str,
        timeout_ms: float = DEFAULT_TIMEOUT_MS,
        retries: RetryPolicy | None = None,
        default_headers: Mapping[str, str] | None = None,
        sdk_name: str = "doctorine-sdk",
        sdk_version: str = "0.0.0",
        idempotency: IdempotencyConfig | None = None,
        now_ms: Callable[[], float] | None = None,
    ) -> "ClientCore":
        return ClientCore(
            base_url=base_url.rstrip("/"),
            user_agent_value=user_agent(sdk_name, sdk_version),
            timeout_ms=timeout_ms,
            retry_policy=resolve_retry_policy(retries),
            default_headers=_lowercase_keys(default_headers),
            idempotency=idempotency if idempotency is not None else IdempotencyConfig(),
            now_ms=now_ms if now_ms is not None else _wall_clock_ms,
        )

    def build_url(
        self,
        path: str,
        query: Mapping[str, QueryParamValue] | None,
        query_styles: Mapping[str, QueryStyle] | None,
    ) -> str:
        base = path if path.startswith(("http://", "https://")) else (
            self.base_url + ("" if path.startswith("/") else "/") + path
        )
        return append_query(base, encode_query(query, query_styles) if query is not None else "")

    def prepare(
        self,
        *,
        method: str,
        path: str,
        query: Mapping[str, QueryParamValue] | None = None,
        query_styles: Mapping[str, QueryStyle] | None = None,
        headers: Mapping[str, str] | None = None,
        body: Any = None,
        content: "bytes | str | None" = None,
        data: list[tuple[str, str]] | None = None,
        files: list[HttpxFileEntry] | None = None,
        timeout_ms: float | None = None,
        idempotency_key: str | None = None,
    ) -> PreparedRequest:
        merged: dict[str, str] = {
            "accept": "application/json",
            "user-agent": self.user_agent_value,
            **_lowercase_keys(self.default_headers),
            **_lowercase_keys(headers),
        }
        resolved_content = content
        if resolved_content is None and data is None and files is None and body is not None:
            encoded, content_type = json_body(body)
            resolved_content = encoded
            merged.setdefault("content-type", content_type)
        # ONE key per logical request -- retries of this call all reuse it.
        key = resolve_idempotency_key(method, idempotency_key, self.idempotency)
        if key is not None:
            merged[self.idempotency.header.lower()] = key
        return PreparedRequest(
            method=method,
            url=self.build_url(path, query, query_styles),
            headers=merged,
            content=resolved_content,
            data=data,
            files=files,
            timeout_ms=timeout_ms if timeout_ms is not None else self.timeout_ms,
        )

    def attempt_url_and_headers(self, prepared: PreparedRequest, material: AuthMaterial) -> tuple[str, dict[str, str]]:
        """Fold one attempt's fresh auth material into the prepared request."""
        url = prepared.url
        if material.query:
            url = append_query(url, encode_query(dict(material.query)))
        return url, {**prepared.headers, **_lowercase_keys(material.headers)}

    def decide(
        self,
        *,
        status: int | None,
        retry_after_header: str | None,
        attempt: int,
        elapsed_ms: float,
    ) -> RetryDecision:
        retry_after_ms = parse_retry_after_ms(retry_after_header, self.now_ms())
        return next_retry_decision(
            self.retry_policy,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            status=status,
            retry_after_ms=retry_after_ms,
        )


def error_from_response(response: httpx.Response) -> APIError:
    """A READ non-2xx response -> the most specific typed error."""
    try:
        body = parse_response_body(response)
    except Exception:  # noqa: BLE001 -- an unparseable error body is still an APIError
        body = None
    return APIError.generate(
        status=response.status_code,
        headers=headers_to_dict(response.headers),
        body=body,
    )


class SyncClient:
    """The synchronous transport orchestrator (implements SyncPageFetcher)."""

    def __init__(
        self,
        *,
        base_url: str,
        auth: AuthProvider | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout_ms: float = DEFAULT_TIMEOUT_MS,
        retries: RetryPolicy | None = None,
        default_headers: Mapping[str, str] | None = None,
        sdk_name: str = "doctorine-sdk",
        sdk_version: str = "0.0.0",
        idempotency: IdempotencyConfig | None = None,
        sleep_ms: Callable[[float], None] | None = None,
        now_ms: Callable[[], float] | None = None,
    ) -> None:
        self._core = ClientCore.create(
            base_url=base_url,
            timeout_ms=timeout_ms,
            retries=retries,
            default_headers=default_headers,
            sdk_name=sdk_name,
            sdk_version=sdk_version,
            idempotency=idempotency,
            now_ms=now_ms,
        )
        self._auth = auth
        self._http = httpx.Client(transport=transport)
        self._sleep_ms: Callable[[float], None] = sleep_ms if sleep_ms is not None else _default_sleep_ms

    def request(self, method: str, path: str, **options: Any) -> APIResponse[Any]:
        """Perform a request and parse the body (JSON / text / bytes)."""
        response = self.request_raw(method, path, **options)
        return APIResponse(
            status=response.status_code,
            headers=headers_to_dict(response.headers),
            data=parse_response_body(response),
        )

    def request_stream(self, method: str, path: str, **options: Any) -> httpx.Response:
        """Full auth/retry pipeline, returning the UNREAD ok response -- the
        entry point streaming methods build on
        (``Stream.from_sse(client.request_stream(...))``)."""
        return self.request_raw(method, path, _stream=True, **options)

    def request_raw(self, method: str, path: str, _stream: bool = False, **options: Any) -> httpx.Response:
        prepared = self._core.prepare(method=method, path=path, **options)
        start_ms = self._core.now_ms()
        attempt = 0
        reauthorized = False
        while True:
            response = self._attempt_once(prepared, stream=_stream)
            if isinstance(response, httpx.Response) and response.is_success:
                return response
            status = response.status_code if isinstance(response, httpx.Response) else None
            if status == 401 and not reauthorized and self._invalidate_auth():
                reauthorized = True
                _discard(response)
                continue
            retry_after = response.headers.get("retry-after") if isinstance(response, httpx.Response) else None
            decision = self._core.decide(
                status=status,
                retry_after_header=retry_after,
                attempt=attempt,
                elapsed_ms=self._core.now_ms() - start_ms,
            )
            if not decision.retry:
                if isinstance(response, httpx.Response):
                    response.read()
                    raise error_from_response(response)
                raise response
            _discard(response)
            self._sleep_ms(decision.delay_ms)
            attempt += 1

    def request_page(self, request: PageRequest) -> Any:
        """The pagination hook (`SyncPageFetcher`)."""
        result = self.request(
            request.method,
            request.url,
            query=dict(request.query),
            headers=dict(request.headers) if request.headers is not None else None,
            body=request.body,
        )
        return result.data

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SyncClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _attempt_once(self, prepared: PreparedRequest, *, stream: bool) -> "httpx.Response | APIConnectionError":
        """One attempt: fresh auth material, one send, taxonomy-mapped
        failures returned (never raised) so the loop decides."""
        material = self._auth.authorize() if self._auth is not None else AuthMaterial()
        url, headers = self._core.attempt_url_and_headers(prepared, material)
        request = self._http.build_request(
            prepared.method,
            url,
            headers=headers,
            content=prepared.content,
            data=dict(prepared.data) if prepared.data is not None else None,
            files=prepared.files,
            timeout=request_timeout(prepared.timeout_ms),
        )
        try:
            return self._http.send(request, stream=stream)
        except httpx.TransportError as error:
            return map_transport_error(error, prepared.timeout_ms)

    def _invalidate_auth(self) -> bool:
        return self._auth.invalidate() if self._auth is not None else False


def _discard(response: "httpx.Response | APIConnectionError") -> None:
    if isinstance(response, httpx.Response):
        response.close()


def _default_sleep_ms(delay_ms: float) -> None:
    time.sleep(delay_ms / 1000.0)
