"""Low-level transport glue over httpx: the mapping from httpx runtime
failures onto the kernel error taxonomy, header flattening, and response
body parsing (JSON -> value, text -> str, else bytes). One httpx send is one
attempt; the retry loop lives in the clients. The kernel targets httpx's
sync AND async clients with a per-request `httpx.Timeout` (the TS kernel's
per-attempt AbortController, one runtime over).

Vendored kernel file -- imports only sibling kernel modules (+ httpx).
"""

from __future__ import annotations

import json
from typing import Any, Final, Literal

import httpx

from ._errors import APIConnectionError, APIConnectionTimeoutError

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"]

DEFAULT_TIMEOUT_MS: Final[float] = 60_000


def headers_to_dict(headers: httpx.Headers) -> dict[str, str]:
    """Flatten httpx headers into a plain lower-cased dict."""
    return {key.lower(): value for key, value in headers.items()}


def map_transport_error(error: Exception, timeout_ms: float) -> APIConnectionError:
    """Map an httpx transport failure onto the kernel taxonomy: timeout ->
    `APIConnectionTimeoutError`; anything else without an HTTP response ->
    `APIConnectionError`."""
    if isinstance(error, httpx.TimeoutException):
        return APIConnectionTimeoutError(timeout_ms)
    return APIConnectionError("Connection error while contacting the API", error)


def request_timeout(timeout_ms: float) -> httpx.Timeout:
    """A per-request httpx timeout from milliseconds."""
    return httpx.Timeout(timeout_ms / 1000.0)


def parse_response_body(response: httpx.Response) -> Any:
    """Parse a READ response body by content type: JSON -> value, text ->
    str, else bytes. 204 -> None; an empty JSON body -> None."""
    if response.status_code == 204:
        return None
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type or "+json" in content_type:
        text = response.text
        return None if text == "" else json.loads(text)
    if content_type.startswith("text/") or content_type == "":
        return response.text
    return response.content
