"""Retry policy + backoff math (doc 38 SS3.2 row 4). Retries are ON BY
DEFAULT (max 2), mirroring the ``retries:`` block of ``doctorine.sdk.yml``
field for field -- config only tunes what the kernel already does. Backoff is
pure exponential WITHOUT jitter: the kernel never draws randomness, so retry
schedules are reproducible (the determinism doctrine extended to runtime).
``Retry-After`` (seconds or HTTP-date) is respected when the server sends it.

Vendored kernel file -- everything here is a pure function.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Final, Literal, Union

# A literal status code or a whole 4XX/5XX family.
StatusMatcher = Union[int, Literal["4XX", "5XX"]]


@dataclass(frozen=True)
class RetryPolicy:
    """Mirrors the ``retries:`` defaults in the sdk-config schema."""

    enabled: bool = True
    max_retries: int = 2
    initial_delay_ms: float = 500
    max_delay_ms: float = 8_000
    max_elapsed_ms: float = 60_000
    exponent: float = 2
    status_codes: tuple[StatusMatcher, ...] = (408, 429, "5XX")
    retry_connection_errors: bool = True
    retry_unsafe_requests: bool = False


DEFAULT_RETRY_POLICY: Final[RetryPolicy] = RetryPolicy()


def resolve_retry_policy(overrides: RetryPolicy | None = None, **fields: Any) -> RetryPolicy:
    """Overlay partial keyword overrides on the default policy."""
    base = overrides if overrides is not None else DEFAULT_RETRY_POLICY
    return replace(base, **fields) if fields else base


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    delay_ms: float


_NO_RETRY: Final[RetryDecision] = RetryDecision(retry=False, delay_ms=0)


def status_matches(status: int, matcher: StatusMatcher) -> bool:
    if isinstance(matcher, int):
        return status == matcher
    if matcher == "4XX":
        return 400 <= status < 500
    return 500 <= status < 600


def is_retryable(policy: RetryPolicy, *, status: int | None) -> bool:
    """``status=None`` means the attempt failed without an HTTP response."""
    if status is None:
        return policy.retry_connection_errors
    return any(status_matches(status, matcher) for matcher in policy.status_codes)


def backoff_delay_ms(policy: RetryPolicy, retry_index: int, retry_after_ms: float | None = None) -> float:
    """The delay before retry number ``retry_index`` (0-based). A server
    ``Retry-After`` overrides the exponential schedule (capped at
    ``max_elapsed_ms``); otherwise ``initial * exponent**i`` capped at
    ``max_delay_ms``.
    """
    if retry_after_ms is not None:
        return min(max(retry_after_ms, 0), policy.max_elapsed_ms)
    return min(policy.initial_delay_ms * policy.exponent**retry_index, policy.max_delay_ms)


def next_retry_decision(
    policy: RetryPolicy,
    *,
    attempt: int,
    elapsed_ms: float,
    status: int | None,
    retry_after_ms: float | None = None,
) -> RetryDecision:
    """The single retry-decision entry point the client transport consults.
    ``attempt`` is the 0-based count of retries already performed;
    ``elapsed_ms`` is wall-clock time since the first attempt started.
    """
    if not policy.enabled or attempt >= policy.max_retries:
        return _NO_RETRY
    if not is_retryable(policy, status=status):
        return _NO_RETRY
    delay_ms = backoff_delay_ms(policy, attempt, retry_after_ms)
    if elapsed_ms + delay_ms > policy.max_elapsed_ms:
        return _NO_RETRY
    return RetryDecision(retry=True, delay_ms=delay_ms)


def parse_retry_after_ms(value: str | None, now_ms: float) -> float | None:
    """Parse a ``Retry-After`` header: delta-seconds (``"3"``) or an
    IMF-fixdate (``"Wed, 01 Jul 2026 10:00:00 GMT"``), returned as
    non-negative ms from ``now_ms``. Unparseable values yield ``None``
    (fall back to backoff).
    """
    if value is None:
        return None
    trimmed = value.strip()
    if trimmed.isdigit():
        return float(trimmed) * 1000
    try:
        at = parsedate_to_datetime(trimmed)
    except (ValueError, TypeError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return max(0.0, at.timestamp() * 1000 - now_ms)


def safe_to_retry(method: str, idempotency_key: str | None, policy: RetryPolicy) -> bool:
    """Non-idempotent writes need a stable key or an explicit retry opt-in."""
    return (method.upper() in {"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"}
            or bool(idempotency_key and idempotency_key.strip()) or policy.retry_unsafe_requests)
