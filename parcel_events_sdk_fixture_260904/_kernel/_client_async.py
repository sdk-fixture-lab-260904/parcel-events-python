"""The core async client: the same one-request pipeline as `_client.py`
(prepare -> retry loop with per-attempt auth/timeout/backoff -> typed error
or parsed body) over ``httpx.AsyncClient``. All request preparation and
retry decisions are the SAME `ClientCore` -- only the I/O verbs differ, so
sync/async behavior cannot drift. Sleeps go through anyio (httpx's own
async substrate), keeping the kernel event-loop-agnostic (asyncio + trio).

Vendored kernel file -- imports only sibling kernel modules (+ httpx/anyio).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping

import anyio
import httpx

from ._auth import AuthMaterial, AuthProvider
from ._client import APIResponse, ClientCore, PreparedRequest, error_from_response
from ._errors import APIConnectionError
from ._idempotency import IdempotencyConfig
from ._pagination_locators import PageRequest
from ._retries import RetryPolicy
from ._transport import (
    DEFAULT_TIMEOUT_MS,
    headers_to_dict,
    map_transport_error,
    parse_response_body,
    request_timeout,
)


class AsyncClient:
    """The asynchronous transport orchestrator (implements AsyncPageFetcher)."""

    def __init__(
        self,
        *,
        base_url: str,
        auth: AuthProvider | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_ms: float = DEFAULT_TIMEOUT_MS,
        retries: RetryPolicy | None = None,
        default_headers: Mapping[str, str] | None = None,
        sdk_name: str = "doctorine-sdk",
        sdk_version: str = "0.0.0",
        idempotency: IdempotencyConfig | None = None,
        sleep_ms: Callable[[float], Awaitable[None]] | None = None,
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
        self._http = httpx.AsyncClient(transport=transport)
        self._sleep_ms: Callable[[float], Awaitable[None]] = (
            sleep_ms if sleep_ms is not None else _default_sleep_ms
        )

    async def request(self, method: str, path: str, **options: Any) -> APIResponse[Any]:
        """Perform a request and parse the body (JSON / text / bytes)."""
        response = await self.request_raw(method, path, **options)
        return APIResponse(
            status=response.status_code,
            headers=headers_to_dict(response.headers),
            data=parse_response_body(response),
        )

    async def request_stream(self, method: str, path: str, **options: Any) -> httpx.Response:
        """Full auth/retry pipeline, returning the UNREAD ok response -- the
        entry point streaming methods build on
        (``AsyncStream.from_sse(await client.request_stream(...))``)."""
        return await self.request_raw(method, path, _stream=True, **options)

    async def request_raw(self, method: str, path: str, _stream: bool = False, **options: Any) -> httpx.Response:
        prepared = self._core.prepare(method=method, path=path, **options)
        start_ms = self._core.now_ms()
        attempt = 0
        reauthorized = False
        while True:
            response = await self._attempt_once(prepared, stream=_stream)
            if isinstance(response, httpx.Response) and response.is_success:
                return response
            status = response.status_code if isinstance(response, httpx.Response) else None
            if status == 401 and not reauthorized and self._invalidate_auth():
                reauthorized = True
                await _adiscard(response)
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
                    await response.aread()
                    raise error_from_response(response)
                raise response
            await _adiscard(response)
            await self._sleep_ms(decision.delay_ms)
            attempt += 1

    async def request_page(self, request: PageRequest) -> Any:
        """The pagination hook (`AsyncPageFetcher`)."""
        result = await self.request(
            request.method,
            request.url,
            query=dict(request.query),
            headers=dict(request.headers) if request.headers is not None else None,
            body=request.body,
        )
        return result.data

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def _attempt_once(
        self, prepared: PreparedRequest, *, stream: bool
    ) -> "httpx.Response | APIConnectionError":
        """One attempt: fresh auth material, one send, taxonomy-mapped
        failures returned (never raised) so the loop decides."""
        material = await self._auth.authorize_async() if self._auth is not None else AuthMaterial()
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
            return await self._http.send(request, stream=stream)
        except httpx.TransportError as error:
            return map_transport_error(error, prepared.timeout_ms)

    def _invalidate_auth(self) -> bool:
        return self._auth.invalidate() if self._auth is not None else False


async def _adiscard(response: "httpx.Response | APIConnectionError") -> None:
    if isinstance(response, httpx.Response):
        await response.aclose()


async def _default_sleep_ms(delay_ms: float) -> None:
    await anyio.sleep(delay_ms / 1000.0)
