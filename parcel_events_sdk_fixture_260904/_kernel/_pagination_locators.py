"""Pagination locator primitives (doc 38 SS3.2 row 2) -- the
``doctorine.sdk.yml`` role-mapping grammar shared by every page scheme:
request locators (``query.cursor``, ``body.page``, ``header.x-next``) and
``$``-rooted dotted response body pointers (``$.data``,
``$.result_info.cursor``). Mirrors the TS kernel's
`pagination-locators.ts` decision for decision.

Vendored kernel file -- imports nothing (stdlib only).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Union

PageQueryValue = Union[str, int, float, bool]


@dataclass(frozen=True)
class PageRequest:
    """One page fetch: path relative to the client's base URL, or an
    absolute URL (the `cursor_url` scheme)."""

    method: str
    url: str
    query: Mapping[str, PageQueryValue] = field(default_factory=dict)
    headers: Mapping[str, str] | None = None
    body: Any = None


def read_body_pointer(body: Any, pointer: str) -> Any:
    """Read a ``$``-rooted dotted body pointer (``$.data.items``)."""
    if pointer != "$" and not pointer.startswith("$."):
        raise ValueError(f"invalid body pointer: {pointer}")
    segments = [] if pointer == "$" else pointer[2:].split(".")
    current: Any = body
    for segment in segments:
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
    return current


def _split_locator(locator: str) -> tuple[str, str]:
    dot = locator.find(".")
    if dot == -1:
        return locator, ""
    return locator[:dot], locator[dot + 1 :]


def _deep_set(target: Any, path: list[str], value: Any) -> Any:
    if not path:
        return value
    base: dict[str, Any] = dict(target) if isinstance(target, dict) else {}
    head = path[0]
    base[head] = _deep_set(base.get(head), path[1:], value)
    return base


def apply_request_locator(request: PageRequest, locator: str, value: PageQueryValue) -> PageRequest:
    """Immutably set a request locator (``query.cursor`` / ``header.x`` /
    ``body.a.b``)."""
    kind, rest = _split_locator(locator)
    if kind == "query":
        return replace(request, query={**dict(request.query), rest: value})
    if kind == "header":
        headers = dict(request.headers) if request.headers is not None else {}
        headers[rest] = _to_header_str(value)
        return replace(request, headers=headers)
    if kind == "body":
        return replace(request, body=_deep_set(request.body, rest.split("."), value))
    if kind == "path":
        raise ValueError("path.* locators cannot advance pagination at runtime")
    raise ValueError(f"unknown request locator kind: {kind}")


def _to_header_str(value: PageQueryValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def read_request_locator(request: PageRequest, locator: str) -> Any:
    """Read the current value at a request locator (offset / page_number
    schemes)."""
    kind, rest = _split_locator(locator)
    if kind == "query":
        return request.query.get(rest)
    if kind == "header":
        return request.headers.get(rest) if request.headers is not None else None
    if kind == "body":
        current: Any = request.body
        for segment in rest.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(segment)
        return current
    return None


def items_at(body: Any, pointer: str) -> list[Any]:
    """The item list at a response pointer ([] when absent / not a list)."""
    value = read_body_pointer(body, pointer)
    return value if isinstance(value, list) else []


def to_finite_number(value: Any) -> float | None:
    """A finite number from a number-or-numeric-string, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None
