"""Pagination pages (doc 38 SS3.2 row 2) -- one scheme object per
``doctorine.sdk.yml`` scheme: cursor / cursor_url / offset / page_number /
token / single_page / item_cursor, shared by `SyncPage` and `AsyncPage` so
the advancing logic exists exactly once. ``for item in client.x.list()`` /
``async for item in ...`` transparently walks ALL pages (a `single_page`
walks its ONE response -- the iteration surface stays uniform across every
list method). Bindings mirror the config's role mappings verbatim.

Vendored kernel file -- imports only sibling kernel modules.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, AsyncIterator, Callable, Generic, Iterator, Protocol, TypeVar

from ._pagination_locators import (
    PageRequest,
    apply_request_locator,
    items_at,
    read_body_pointer,
    read_request_locator,
    to_finite_number,
)

ItemT = TypeVar("ItemT")


class PageScheme(Protocol):
    """The pure per-scheme logic both page classes delegate to."""

    def items(self, body: Any) -> list[Any]: ...

    def next_request(self, request: PageRequest, body: Any) -> PageRequest | None: ...


# -- schemes -----------------------------------------------------------------


@dataclass(frozen=True)
class CursorScheme:
    request_cursor: str
    response_items: str
    response_next_cursor: str

    def items(self, body: Any) -> list[Any]:
        return items_at(body, self.response_items)

    def next_request(self, request: PageRequest, body: Any) -> PageRequest | None:
        cursor = read_body_pointer(body, self.response_next_cursor)
        if cursor is None or cursor == "" or isinstance(cursor, bool):
            return None
        if not isinstance(cursor, (str, int, float)):
            return None
        return apply_request_locator(request, self.request_cursor, cursor)


@dataclass(frozen=True)
class TokenScheme:
    """Google-style page tokens: cursor semantics, different role names."""

    request_page_token: str
    response_items: str
    response_next_page_token: str

    def _as_cursor(self) -> CursorScheme:
        return CursorScheme(
            request_cursor=self.request_page_token,
            response_items=self.response_items,
            response_next_cursor=self.response_next_page_token,
        )

    def items(self, body: Any) -> list[Any]:
        return self._as_cursor().items(body)

    def next_request(self, request: PageRequest, body: Any) -> PageRequest | None:
        return self._as_cursor().next_request(request, body)


@dataclass(frozen=True)
class CursorUrlScheme:
    """The next page is a ready-made URL."""

    response_items: str
    response_next_url: str

    def items(self, body: Any) -> list[Any]:
        return items_at(body, self.response_items)

    def next_request(self, request: PageRequest, body: Any) -> PageRequest | None:
        next_url = read_body_pointer(body, self.response_next_url)
        if not isinstance(next_url, str) or next_url == "":
            return None
        # The next URL carries its own query string -- clear the request's.
        return replace(request, url=next_url, query={})


@dataclass(frozen=True)
class OffsetScheme:
    request_offset: str
    response_items: str
    response_total: str | None = None

    def items(self, body: Any) -> list[Any]:
        return items_at(body, self.response_items)

    def next_request(self, request: PageRequest, body: Any) -> PageRequest | None:
        count = len(self.items(body))
        if count == 0:
            return None
        current = to_finite_number(read_request_locator(request, self.request_offset)) or 0.0
        next_offset = int(current) + count
        if self.response_total is not None:
            total = to_finite_number(read_body_pointer(body, self.response_total))
            if total is not None and next_offset >= total:
                return None
        return apply_request_locator(request, self.request_offset, next_offset)


@dataclass(frozen=True)
class PageNumberScheme:
    request_page: str
    response_items: str
    response_total_pages: str | None = None

    def items(self, body: Any) -> list[Any]:
        return items_at(body, self.response_items)

    def next_request(self, request: PageRequest, body: Any) -> PageRequest | None:
        if len(self.items(body)) == 0:
            return None
        current = to_finite_number(read_request_locator(request, self.request_page))
        next_page = int(current if current is not None else 1.0) + 1
        if self.response_total_pages is not None:
            total_pages = to_finite_number(read_body_pointer(body, self.response_total_pages))
            if total_pages is not None and next_page > total_pages:
                return None
        return apply_request_locator(request, self.request_page, next_page)


@dataclass(frozen=True)
class SinglePageScheme:
    """Non-advancing: the one response IS the collection (iteration walks
    its items and stops -- there is never a next page)."""

    response_items: str

    def items(self, body: Any) -> list[Any]:
        return items_at(body, self.response_items)

    def next_request(self, request: PageRequest, body: Any) -> PageRequest | None:
        return None


@dataclass(frozen=True)
class ItemCursorScheme:
    """The LAST item's field value is the next cursor."""

    request_cursor: str
    response_items: str
    response_has_more: str
    #: Item field whose last value advances the cursor (``id``).
    item_cursor_field: str

    def items(self, body: Any) -> list[Any]:
        return items_at(body, self.response_items)

    def next_request(self, request: PageRequest, body: Any) -> PageRequest | None:
        if read_body_pointer(body, self.response_has_more) is not True:
            return None
        items = self.items(body)
        if not items or not isinstance(items[-1], dict):
            return None
        cursor = items[-1].get(self.item_cursor_field)
        if isinstance(cursor, bool) or not isinstance(cursor, (str, int, float)):
            return None
        return apply_request_locator(request, self.request_cursor, cursor)


# -- the page classes --------------------------------------------------------


class SyncPageFetcher(Protocol):
    """The client hook sync pages fetch through (SyncClient implements it)."""

    def request_page(self, request: PageRequest) -> Any: ...


class AsyncPageFetcher(Protocol):
    """The client hook async pages fetch through (AsyncClient implements it)."""

    async def request_page(self, request: PageRequest) -> Any: ...


def _identity(value: Any) -> Any:
    return value


class SyncPage(Generic[ItemT]):
    """One fetched page; ``for item in page`` walks ALL subsequent pages."""

    def __init__(
        self,
        fetcher: SyncPageFetcher,
        request: PageRequest,
        body: Any,
        scheme: PageScheme,
        cast_item: Callable[[Any], ItemT] | None = None,
        validate_body: Callable[[Any], Any] | None = None,
    ) -> None:
        if validate_body is not None:
            validate_body(body)
        self._validate_body = validate_body
        self._fetcher = fetcher
        self.request = request
        self.body = body
        self._scheme = scheme
        self._cast_item: Callable[[Any], ItemT] = cast_item if cast_item is not None else _identity

    def items(self) -> list[ItemT]:
        return [self._cast_item(item) for item in self._scheme.items(self.body)]

    def next_page_request(self) -> PageRequest | None:
        return self._scheme.next_request(self.request, self.body)

    def has_next_page(self) -> bool:
        return len(self._scheme.items(self.body)) > 0 and self.next_page_request() is not None

    def get_next_page(self) -> "SyncPage[ItemT]":
        next_request = self.next_page_request()
        if next_request is None:
            raise RuntimeError("no next page; check has_next_page() first")
        body = self._fetcher.request_page(next_request)
        return SyncPage(self._fetcher, next_request, body, self._scheme, self._cast_item, self._validate_body)

    def __iter__(self) -> Iterator[ItemT]:
        page: SyncPage[ItemT] | None = self
        while page is not None:
            yield from page.items()
            page = page.get_next_page() if page.has_next_page() else None


class AsyncPage(Generic[ItemT]):
    """One fetched page; ``async for item in page`` walks ALL pages."""

    def __init__(
        self,
        fetcher: AsyncPageFetcher,
        request: PageRequest,
        body: Any,
        scheme: PageScheme,
        cast_item: Callable[[Any], ItemT] | None = None,
        validate_body: Callable[[Any], Any] | None = None,
    ) -> None:
        if validate_body is not None:
            validate_body(body)
        self._validate_body = validate_body
        self._fetcher = fetcher
        self.request = request
        self.body = body
        self._scheme = scheme
        self._cast_item: Callable[[Any], ItemT] = cast_item if cast_item is not None else _identity

    def items(self) -> list[ItemT]:
        return [self._cast_item(item) for item in self._scheme.items(self.body)]

    def next_page_request(self) -> PageRequest | None:
        return self._scheme.next_request(self.request, self.body)

    def has_next_page(self) -> bool:
        return len(self._scheme.items(self.body)) > 0 and self.next_page_request() is not None

    async def get_next_page(self) -> "AsyncPage[ItemT]":
        next_request = self.next_page_request()
        if next_request is None:
            raise RuntimeError("no next page; check has_next_page() first")
        body = await self._fetcher.request_page(next_request)
        return AsyncPage(self._fetcher, next_request, body, self._scheme, self._cast_item, self._validate_body)

    async def __aiter__(self) -> AsyncIterator[ItemT]:
        page: AsyncPage[ItemT] | None = self
        while page is not None:
            for item in page.items():
                yield item
            page = await page.get_next_page() if page.has_next_page() else None
