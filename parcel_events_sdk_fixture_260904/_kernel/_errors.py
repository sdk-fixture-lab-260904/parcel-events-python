"""The kernel error taxonomy (doc 38 SS3.2 row 6): a typed `APIError` base
with status-family subclasses (``except RateLimitError:``), plus the non-HTTP
failures -- `APIConnectionError` (network) and `APIConnectionTimeoutError`
(per-request timeout). Error bodies are captured verbatim on the exception;
`x-request-id` is surfaced as `request_id` for support tickets. Mirrors the
TS kernel's `errors.ts` field for field.

Vendored kernel file -- imports nothing (self-contained by construction).
"""

from __future__ import annotations

from typing import Any, Mapping


class APIError(Exception):
    """Base class for every non-2xx HTTP response the API returned."""

    status: int
    headers: Mapping[str, str]
    #: The parsed (JSON) or raw (text) error body, captured verbatim.
    body: Any
    #: The ``x-request-id`` response header, when the server sent one.
    request_id: str | None

    def __init__(
        self,
        *,
        status: int,
        headers: Mapping[str, str] | None = None,
        body: Any = None,
        message: str | None = None,
    ) -> None:
        super().__init__(message if message is not None else _default_error_message(status, body))
        self.status = status
        self.headers = dict(headers) if headers is not None else {}
        self.body = body
        self.request_id = self.headers.get("x-request-id")

    @staticmethod
    def generate(
        *,
        status: int,
        headers: Mapping[str, str] | None = None,
        body: Any = None,
        message: str | None = None,
    ) -> "APIError":
        """Map a status onto the most specific error subclass."""
        cls = _STATUS_CLASSES.get(status)
        if cls is None:
            cls = InternalServerError if status >= 500 else APIError
        return cls(status=status, headers=headers, body=body, message=message)


class BadRequestError(APIError):
    pass


class AuthenticationError(APIError):
    pass


class PermissionDeniedError(APIError):
    pass


class NotFoundError(APIError):
    pass


class ConflictError(APIError):
    pass


class UnprocessableEntityError(APIError):
    pass


class RateLimitError(APIError):
    pass


class InternalServerError(APIError):
    pass


_STATUS_CLASSES: dict[int, type[APIError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: UnprocessableEntityError,
    429: RateLimitError,
}


class APIConnectionError(Exception):
    """The request never produced an HTTP response (DNS, TLS, socket, ...)."""

    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.__cause__ = cause


class APIConnectionTimeoutError(APIConnectionError):
    """The per-request timeout elapsed before a response arrived."""

    timeout_ms: float

    def __init__(self, timeout_ms: float) -> None:
        super().__init__(f"Request timed out after {timeout_ms:g} ms")
        self.timeout_ms = timeout_ms


def _default_error_message(status: int, body: Any) -> str:
    """Best-effort human message from common ``{error:{message}}`` bodies."""
    detail = _extract_message(body)
    return f"HTTP {status}" if detail is None else f"HTTP {status}: {detail}"


def _extract_message(body: Any) -> str | None:
    if isinstance(body, str):
        return body if len(body) > 0 else None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, str) and len(error) > 0:
        return error
    if isinstance(error, dict):
        nested = error.get("message")
        if isinstance(nested, str) and len(nested) > 0:
            return nested
    message = body.get("message")
    if isinstance(message, str) and len(message) > 0:
        return message
    return None
