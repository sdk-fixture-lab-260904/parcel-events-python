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
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Sequence, Union, TypedDict
from urllib.parse import quote

QueryPrimitive = Union[str, int, float, bool]
QueryParamValue = Union[
    QueryPrimitive,
    Sequence[QueryPrimitive],
    Mapping[str, QueryPrimitive],
    None,
]

LegacyQueryStyle = Literal["form", "form_no_explode", "space_delimited", "pipe_delimited", "deep_object"]


class ParameterSerialization(TypedDict, total=False):
    style: str
    explode: bool
    allowReserved: bool
    contentType: str


QueryStyle = Union[LegacyQueryStyle, ParameterSerialization]

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


def _encode_sequence(pairs: list[str], key: str, values: Sequence[QueryPrimitive], style: LegacyQueryStyle) -> None:
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


def _encode_mapping(pairs: list[str], key: str, value: Mapping[str, QueryPrimitive], style: LegacyQueryStyle) -> None:
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
        style: QueryStyle = styles.get(key, "form") if styles is not None else "form"
        if value is None and not (isinstance(style, dict) and "contentType" in style):
            continue
        if isinstance(style, dict):
            pairs.extend(_parameter_query(key, value, style))
        elif isinstance(value, Mapping):
            _encode_mapping(pairs, key, value, style)
        elif isinstance(value, (str, bool, int, float)):
            pairs.append(_pair(key, value))
        elif value is not None:
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


# Descriptor codecs consume frozen OpenAPI parameter semantics from FoundryIR.
# https://spec.openapis.org/oas/v3.1.1.html#style-values

def _parameter_scalar(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, bool, int, float)):
        raise TypeError("parameter serialization requires flat scalar values")
    if isinstance(value, float) and (value != value or abs(value) == float("inf")):
        raise ValueError("parameter serialization requires finite numbers")
    return _to_str(value)


def _parameter_quote(value: Any, reserved: bool = False) -> str:
    text = _parameter_scalar(value)
    safe = "-._~" + (":/?@!$'()*,;" if reserved else "")
    # Reserved expansion preserves valid existing percent triplets, but not bare %.
    if reserved:
        return "".join(part if re.fullmatch(r"%[0-9A-Fa-f]{2}", part) else quote(part, safe=safe)
                       for part in re.split(r"(%[0-9A-Fa-f]{2})", text))
    return quote(text, safe=safe)


def _content_value(value: Any, rule: ParameterSerialization) -> Any:
    media = rule.get("contentType")
    if media is None:
        return value
    if media != "application/json" and not media.endswith("+json"):
        raise ValueError("unsupported parameter content type")
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _parts(value: Any, encode: Callable[[Any], str], explode: bool, separator: str) -> str:
    if isinstance(value, Mapping):
        if explode:
            return separator.join(encode(key) + "=" + encode(item) for key, item in value.items())
        return separator.join(encode(item) for pair in value.items() for item in pair)
    if isinstance(value, (list, tuple)):
        return separator.join(encode(item) for item in value)
    return encode(value)


def _parameter_query(name: str, value: Any, rule: ParameterSerialization) -> list[str]:
    value = _content_value(value, rule)
    def encode(item: Any) -> str:
        return _parameter_quote(item, rule.get("allowReserved", False))
    key = _parameter_quote(name)
    if "contentType" in rule:
        return [key + "=" + encode(value)]
    style = rule.get("style", "form")
    explode = rule.get("explode", style == "form")
    if style == "deepObject":
        if not explode or not isinstance(value, Mapping):
            raise TypeError("deepObject requires an object and explode true")
        return [_parameter_quote(name + "[" + str(prop) + "]") + "=" + encode(item)
                for prop, item in value.items()]
    if style not in ("form", "spaceDelimited", "pipeDelimited"):
        raise ValueError("unsupported query parameter style")
    if explode:
        if style != "form":
            raise ValueError("delimited query styles require explode false")
        if isinstance(value, Mapping):
            return [_parameter_quote(prop) + "=" + encode(item) for prop, item in value.items()]
        if isinstance(value, (list, tuple)):
            return [key + "=" + encode(item) for item in value]
    separator = {"form": ",", "spaceDelimited": "%20", "pipeDelimited": "%7C"}[style]
    return [key + "=" + _parts(value, encode, False, separator)]


def encode_path_parameter(name: str, value: Any, rule: ParameterSerialization) -> str:
    value = _content_value(value, rule)
    if "contentType" in rule:
        return _parameter_quote(value)
    style, explode = rule.get("style", "simple"), rule.get("explode", False)
    if style == "simple":
        return _parts(value, _parameter_quote, explode, ",")
    if style == "label":
        return "." + _parts(value, _parameter_quote, explode, "." if explode else ",")
    if style != "matrix":
        raise ValueError("unsupported path parameter style")
    key = _parameter_quote(name)
    if explode and isinstance(value, Mapping):
        return "".join(";" + _parameter_quote(prop) + "=" + _parameter_quote(item)
                       for prop, item in value.items())
    if explode and isinstance(value, (list, tuple)):
        return "".join(";" + key + "=" + _parameter_quote(item) for item in value)
    result = _parts(value, _parameter_quote, False, ",")
    return ";" + key + ("=" + result if result else "")


def encode_header_parameter(value: Any, rule: ParameterSerialization) -> str:
    if rule.get("style", "simple") != "simple":
        raise ValueError("unsupported header parameter style")
    value = _content_value(value, rule)
    result = _parts(value, _parameter_scalar, rule.get("explode", False), ",")
    if any(ord(char) < 32 or ord(char) == 127 for char in result):
        raise ValueError("header parameter contains control characters")
    return result


def encode_cookie_parameter(name: str, value: Any, rule: ParameterSerialization) -> str:
    if rule.get("style", "form") != "form":
        raise ValueError("unsupported cookie parameter style")
    value = _content_value(value, rule)
    key = _parameter_quote(name)
    if "contentType" not in rule and rule.get("explode", True):
        if isinstance(value, Mapping):
            return "; ".join(_parameter_quote(prop) + "=" + _parameter_quote(item)
                             for prop, item in value.items())
        if isinstance(value, (list, tuple)):
            return "; ".join(key + "=" + _parameter_quote(item) for item in value)
    return key + "=" + _parts(value, _parameter_quote, False, ",")
