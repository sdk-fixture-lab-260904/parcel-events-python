"""Streaming readers (doc 38 SS3.2 row 3, ``streaming:`` protocols
``sse|jsonl``): an SSE parser and a JSONL line reader, surfaced through
`Stream` / `AsyncStream` -- single-use iterables of typed events. Python has
no dependent typing, so generated code ships SPLIT ``create`` /
``create_streaming`` method variants and only the streaming variant returns
one of these. ``[DONE]`` (configurable) terminates SSE streams; an early
``break`` closes the underlying response, and an incomplete trailing SSE
event is discarded per the spec.

Vendored kernel file -- imports only sibling kernel modules (+ httpx).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Generic, Iterable, Iterator, TypeVar
from types import TracebackType

import httpx

from ._errors import APIConnectionError
from ._stream_lines import stream_lines, async_stream_lines

EventT = TypeVar("EventT")


@dataclass(frozen=True)
class ServerSentEvent:
    event: str
    data: str
    id: str | None = None
    retry: int | None = None


class _SseParser:
    """WHATWG EventSource field semantics, one line at a time."""

    def __init__(self) -> None:
        self._event = ""
        self._data: list[str] = []
        self._size = 0
        self._first = True
        self._id: str | None = None
        self._retry: int | None = None

    def push_line(self, line: str) -> ServerSentEvent | None:
        if self._first:
            line = line.removeprefix("\ufeff")
            self._first = False
        if len(line) > 8 * 1024 * 1024:
            raise APIConnectionError("Stream line limit exceeded")
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None
        field, value = _split_sse_field(line)
        if field == "event":
            self._event = value
        elif field == "data":
            self._size += len(value) + 1
            if self._size > 8 * 1024 * 1024:
                raise APIConnectionError("Stream event limit exceeded")
            self._data.append(value)
        elif field == "id" and "\x00" not in value:
            self._id = value
        elif field == "retry" and value.isdigit():
            self._retry = int(value)
        return None

    def _dispatch(self) -> ServerSentEvent | None:
        if not self._data:
            self._event = ""
            return None
        event = ServerSentEvent(
            event=self._event if self._event != "" else "message",
            data="\n".join(self._data),
            id=self._id,
            retry=self._retry,
        )
        self._event = ""
        self._data = []
        self._size = 0
        return event


def _split_sse_field(line: str) -> tuple[str, str]:
    colon = line.find(":")
    if colon == -1:
        return line, ""
    raw = line[colon + 1 :]
    return line[:colon], raw[1:] if raw.startswith(" ") else raw


def iter_sse(lines: Iterable[str]) -> Iterator[ServerSentEvent]:
    """Parse SSE lines into events. An incomplete trailing event (no
    dispatching blank line) is discarded per the spec."""
    parser = _SseParser()
    for line in lines:
        event = parser.push_line(line)
        if event is not None:
            yield event


async def aiter_sse(lines: AsyncIterator[str]) -> AsyncIterator[ServerSentEvent]:
    parser = _SseParser()
    async for line in lines:
        event = parser.push_line(line)
        if event is not None:
            yield event


DEFAULT_DONE_SENTINEL = "[DONE]"


def _identity(value: Any) -> Any:
    return value


def _require_media(response: httpx.Response, protocol: str) -> None:
    media = response.headers.get("content-type", "").split(";")[0].strip().lower()
    allowed = {"text/event-stream"} if protocol == "sse" else {"application/jsonl", "application/x-ndjson", "application/ndjson"}
    if media not in allowed:
        raise APIConnectionError("Stream response content type mismatch")


def _require_streamable(response: httpx.Response) -> httpx.Response:
    if response.status_code == 204:
        raise APIConnectionError("Response has no body to stream")
    return response


class Stream(Generic[EventT]):
    """A single-use iterable of typed streaming events (sync)."""

    def __init__(self, source: Callable[[], Iterator[EventT]], close: Callable[[], None] | None = None) -> None:
        self._source = source
        self._consumed = False
        self._close = close
        self._closed = False

    @staticmethod
    def from_sse(
        response: httpx.Response,
        *,
        done_sentinel: str = DEFAULT_DONE_SENTINEL,
        cast_event: Callable[[Any], EventT] | None = None,
    ) -> "Stream[EventT]":
        """Typed events from an SSE response: JSON-parsed ``data``,
        ``[DONE]``-aware. Closes the response when iteration ends."""
        cast = cast_event if cast_event is not None else _identity
        _require_streamable(response)

        def generate() -> Iterator[EventT]:
            try:
                _require_media(response, "sse")
                for sse in iter_sse(stream_lines(response)):
                    if sse.data == done_sentinel:
                        return
                    yield cast(json.loads(sse.data))
            finally:
                response.close()

        return Stream(generate, response.close)

    @staticmethod
    def from_sse_events(response: httpx.Response) -> "Stream[ServerSentEvent]":
        """Raw SSE events (event name, id, retry preserved) -- no JSON."""
        _require_streamable(response)

        def generate() -> Iterator[ServerSentEvent]:
            try:
                _require_media(response, "sse")
                yield from iter_sse(stream_lines(response))
            finally:
                response.close()

        return Stream(generate, response.close)

    @staticmethod
    def from_jsonl(
        response: httpx.Response,
        *,
        cast_event: Callable[[Any], EventT] | None = None,
    ) -> "Stream[EventT]":
        """Typed items from a JSONL response; blank lines are skipped."""
        cast = cast_event if cast_event is not None else _identity
        _require_streamable(response)

        def generate() -> Iterator[EventT]:
            try:
                _require_media(response, "jsonl")
                for line in stream_lines(response):
                    stripped = line.strip()
                    if stripped != "":
                        yield cast(json.loads(stripped))
            finally:
                response.close()

        return Stream(generate, response.close)

    def close(self) -> None:
        """Release the response, including when iteration never started."""
        if not self._closed:
            self._closed = True
            if self._close is not None:
                self._close()

    def __enter__(self) -> "Stream[EventT]":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        self.close()

    def __iter__(self) -> Iterator[EventT]:
        if self._closed:
            raise RuntimeError("this Stream is closed")
        if self._consumed:
            raise RuntimeError("this Stream was already consumed -- iterate it once")
        self._consumed = True
        return self._source()


class AsyncStream(Generic[EventT]):
    """A single-use async iterable of typed streaming events."""

    def __init__(self, source: Callable[[], AsyncIterator[EventT]], close: Callable[[], Awaitable[None]] | None = None) -> None:
        self._source = source
        self._consumed = False
        self._close = close
        self._closed = False

    @staticmethod
    def from_sse(
        response: httpx.Response,
        *,
        done_sentinel: str = DEFAULT_DONE_SENTINEL,
        cast_event: Callable[[Any], EventT] | None = None,
    ) -> "AsyncStream[EventT]":
        cast = cast_event if cast_event is not None else _identity
        _require_streamable(response)

        async def generate() -> AsyncIterator[EventT]:
            try:
                _require_media(response, "sse")
                async for sse in aiter_sse(async_stream_lines(response)):
                    if sse.data == done_sentinel:
                        return
                    yield cast(json.loads(sse.data))
            finally:
                await response.aclose()

        return AsyncStream(generate, response.aclose)

    @staticmethod
    def from_sse_events(response: httpx.Response) -> "AsyncStream[ServerSentEvent]":
        _require_streamable(response)

        async def generate() -> AsyncIterator[ServerSentEvent]:
            try:
                _require_media(response, "sse")
                async for event in aiter_sse(async_stream_lines(response)):
                    yield event
            finally:
                await response.aclose()

        return AsyncStream(generate, response.aclose)

    @staticmethod
    def from_jsonl(
        response: httpx.Response,
        *,
        cast_event: Callable[[Any], EventT] | None = None,
    ) -> "AsyncStream[EventT]":
        cast = cast_event if cast_event is not None else _identity
        _require_streamable(response)

        async def generate() -> AsyncIterator[EventT]:
            try:
                _require_media(response, "jsonl")
                async for line in async_stream_lines(response):
                    stripped = line.strip()
                    if stripped != "":
                        yield cast(json.loads(stripped))
            finally:
                await response.aclose()

        return AsyncStream(generate, response.aclose)

    async def aclose(self) -> None:
        """Release the response, including when iteration never started."""
        if not self._closed:
            self._closed = True
            if self._close is not None:
                await self._close()

    async def __aenter__(self) -> "AsyncStream[EventT]":
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        await self.aclose()

    def __aiter__(self) -> AsyncIterator[EventT]:
        if self._closed:
            raise RuntimeError("this AsyncStream is closed")
        if self._consumed:
            raise RuntimeError("this AsyncStream was already consumed -- iterate it once")
        self._consumed = True
        return self._source()
