"""Idempotency-key injection (doc 38 SS3.2 row 8, ``idempotency:`` in
``doctorine.sdk.yml``). The key is resolved ONCE per logical request, BEFORE
the retry loop, so every retry of one call carries the SAME key -- that is
the whole point of the header. Auto-generation (uuid4) applies only to POST;
an explicit caller key is honored on any method.

Vendored kernel file -- imports only sibling kernel modules.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable, Final

DEFAULT_IDEMPOTENCY_HEADER: Final[str] = "Idempotency-Key"


def default_idempotency_key() -> str:
    """``doctorine-py-<uuid>`` -- runtime-unique by design (never hashed
    into builds)."""
    return f"doctorine-py-{uuid.uuid4()}"


@dataclass(frozen=True)
class IdempotencyConfig:
    """Mirrors the ``idempotency:`` config block."""

    #: Header name (config ``idempotency.header``).
    header: str = DEFAULT_IDEMPOTENCY_HEADER
    #: Auto-generate a key for POST requests (opt-in, like the config block).
    auto_generate: bool = False
    #: Key factory override (default ``doctorine-py-<uuid>``).
    generate: Callable[[], str] = field(default=default_idempotency_key)


def resolve_idempotency_key(
    method: str,
    explicit_key: str | None,
    config: IdempotencyConfig,
) -> str | None:
    """The key for this logical request: an explicit key always wins;
    otherwise auto-generation covers POST only (the one
    non-idempotent-by-spec verb)."""
    if explicit_key is not None and explicit_key != "":
        return explicit_key
    if config.auto_generate and method == "POST":
        return config.generate()
    return None
