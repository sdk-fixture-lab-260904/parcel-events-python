"""Serialization helpers (doc 38 SS3.2 rows 7-8): OpenAPI query-encoding
styles (form / form_no_explode / space_delimited / pipe_delimited /
deep_object), JSON bodies, multipart/form-data uploads, and base64 (UTF-8
first, mirroring the TS kernel's `encodeBase64`). Percent-encoding uses the
`encodeURIComponent` unreserved set so the TS and Python kernels emit
byte-identical query strings for identical input.

Vendored kernel file -- imports nothing (stdlib only).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence, Union
from urllib.parse import quote

QueryPrimitive = Union[str, int, float, bool]
QueryParamValue = Union[
    QueryPrimitive,
    Sequence[QueryPrimitive],
    Mapping[str, QueryPrimitive],
    None,
]

QueryStyle = Literal["form", "form_no_explode", "space_delimited", "pipe_delimited", "deep_object"]

#: `encodeURIComponent`'s unreserved characters (RFC 3986 + `!'()*`).
_URI_COMPONENT_SAFE: str = "!'()*-._~"


def _to_str(value: QueryPrimitive) -> str:
    """JS `String()` semantics: booleans are lowercase."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _quote(value: str) -> str:
    return quote(value, safe=_URI_COMPONENT_SAFE)


def _pair(key: str, value: QueryPrimitive) -> str:
    return f"{_quote(key)}={_quote(_to_str(value))}"


def _joined(key: str, values: Sequence[QueryPrimitive], separator: str) -> str:
    return f"{_quote(key)}=" + separator.join(_quote(_to_str(v)) for v in values)


def _encode_sequence(pairs: list[str], key: str, values: Sequence[QueryPrimitive], style: QueryStyle) -> None:
    if style == "form":
        pairs.extend(_pair(key, value) for value in values)
    elif style == "form_no_explode":
        pairs.append(_joined(key, values, ","))
    elif style == "space_delimited":
        pairs.append(_joined(key, values, "%20"))
    elif style == "pipe_delimited":
        pairs.append(_joined(key, values, "%7C"))
    else:  # deep_object
        pairs.extend(_pair(f"{key}[{index}]", value) for index, value in enumerate(values))


def _encode_mapping(pairs: list[str], key: str, value: Mapping[str, QueryPrimitive], style: QueryStyle) -> None:
    if style == "form":
        pairs.extend(_pair(prop, prop_value) for prop, prop_value in value.items())
    elif style == "form_no_explode":
        flat: list[QueryPrimitive] = []
        for prop, prop_value in value.items():
            flat.extend((prop, prop_value))
        pairs.append(_joined(key, flat, ","))
    elif style == "deep_object":
        pairs.extend(_pair(f"{key}[{prop}]", prop_value) for prop, prop_value in value.items())
    else:
        raise TypeError(f'query style "{style}" does not support mapping values')


def encode_query(
    params: Mapping[str, QueryParamValue],
    styles: Mapping[str, QueryStyle] | None = None,
) -> str:
    """Encode query params. Style defaults to OpenAPI's `form` (explode):
    sequences repeat the key, mappings flatten to their property names. Key
    order follows the caller's insertion order -- deterministic for
    identical input."""
    pairs: list[str] = []
    for key, value in params.items():
        if value is None:
            continue
        style: QueryStyle = styles.get(key, "form") if styles is not None else "form"
        if isinstance(value, Mapping):
            _encode_mapping(pairs, key, value, style)
        elif isinstance(value, (str, bool, int, float)):
            pairs.append(_pair(key, value))
        else:
            _encode_sequence(pairs, key, value, style)
    return "&".join(pairs)


def append_query(url: str, query: str) -> str:
    """Append ``query`` onto a URL that may already carry a query string."""
    if query == "":
        return url
    return url + ("&" if "?" in url else "?") + query


def json_body(value: Any) -> tuple[str, str]:
    """JSON request body + its content type. Compact separators mirror
    `JSON.stringify` (no whitespace); non-ASCII passes through as UTF-8."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False), "application/json"


# -- multipart upload --------------------------------------------------------


@dataclass(frozen=True)
class UploadPart:
    """One file-ish multipart field value."""

    data: "bytes | str"
    filename: str | None = None
    content_type: str | None = None


MultipartFieldValue = Union[UploadPart, QueryPrimitive]

#: httpx `files=` entry: (field, (filename, content, content_type|None)).
HttpxFileEntry = tuple[str, tuple[str, "bytes | str", "str | None"]]


def split_multipart(
    fields: Mapping[str, Union[MultipartFieldValue, Sequence[MultipartFieldValue]]],
) -> tuple[list[tuple[str, str]], list[HttpxFileEntry]]:
    """Split multipart fields into the (data, files) pair httpx's
    ``multipart/form-data`` encoder consumes -- httpx stamps the boundary
    content-type itself."""
    data: list[tuple[str, str]] = []
    files: list[HttpxFileEntry] = []
    for key, value in fields.items():
        items: Sequence[MultipartFieldValue]
        if isinstance(value, (UploadPart, str, bool, int, float)):
            items = [value]
        else:
            items = value
        for item in items:
            if isinstance(item, UploadPart):
                files.append((key, (item.filename or "upload", item.data, item.content_type)))
            else:
                data.append((key, _to_str(item)))
    return data, files


# -- base64 (RFC 4648) -------------------------------------------------------


def encode_base64(value: "bytes | str") -> str:
    """Base64-encode bytes (strings encode as UTF-8 first)."""
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return base64.b64encode(raw).decode("ascii")
