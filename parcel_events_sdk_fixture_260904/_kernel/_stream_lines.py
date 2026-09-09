"""Bounded UTF-8 line decoding shared by synchronous and asynchronous streams."""
from __future__ import annotations

import codecs
import re
from typing import AsyncIterator, Iterator

import httpx

from ._errors import APIConnectionError


class _Lines:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8-sig")("replace")
        self._buffer = ""

    def feed(self, chunk: bytes, final: bool = False) -> list[str]:
        self._buffer += self._decoder.decode(chunk, final=final)
        if final and self._buffer.endswith("\r"):
            self._buffer += "\n"
        parts = re.split(r"\r\n|\r(?!$)|\n", self._buffer)
        self._buffer = parts.pop()
        if len(self._buffer) > 8 * 1024 * 1024 or any(len(line) > 8 * 1024 * 1024 for line in parts):
            raise APIConnectionError("Stream line limit exceeded")
        if final and self._buffer:
            parts.append(self._buffer)
            self._buffer = ""
        return parts


def stream_lines(response: httpx.Response) -> Iterator[str]:
    lines = _Lines()
    for chunk in response.iter_bytes():
        yield from lines.feed(chunk)
    yield from lines.feed(b"", final=True)


async def async_stream_lines(response: httpx.Response) -> AsyncIterator[str]:
    lines = _Lines()
    async for chunk in response.aiter_bytes():
        for line in lines.feed(chunk):
            yield line
    for line in lines.feed(b"", final=True):
        yield line
