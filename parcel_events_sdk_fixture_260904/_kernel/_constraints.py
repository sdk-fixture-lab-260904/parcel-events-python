"""OpenAPI assertion metadata used by generated Pydantic validators."""
from __future__ import annotations

import math
from fractions import Fraction
from functools import lru_cache
from typing import Annotated, Any, Callable, Mapping

from pydantic import BaseModel, StringConstraints, TypeAdapter
from ._formats import matches_format


class ConstraintLimitError(RuntimeError):
    """Fatal resource limit; union branches must never suppress this failure."""


def constraint_validator(constraints: Mapping[str, Any]) -> Callable[[Any], Any]:
    def validate(value: Any) -> Any:
        validate_constraints(value, constraints)
        return value
    return validate


def validate_constraints(value: Any, constraints: Mapping[str, Any], depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [100000]
    budget[0] -= 1
    if depth > 128 or budget[0] < 0:
        raise ConstraintLimitError("constraint nesting limit")
    for part in constraints.get("allOf", []):
        validate_constraints(value, part, depth + 1, budget)
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True, exclude_unset=True)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        _number_constraints(value, constraints)
    elif isinstance(value, str):
        if "format" in constraints and not matches_format(constraints["format"], value):
            raise ValueError("format " + constraints["format"])
        _size(len(value), constraints, "minLength", "maxLength")
        if "pattern" in constraints:
            _pattern_adapter(constraints["pattern"]).validate_python(value, strict=True)
    elif isinstance(value, Mapping):
        _size(len(value), constraints, "minProperties", "maxProperties")
    elif isinstance(value, (list, tuple)):
        _size(len(value), constraints, "minItems", "maxItems")
        if constraints.get("uniqueItems") is True:
            keys = [_unique_key(item, 0, budget) for item in value]
            if len(set(keys)) != len(keys):
                raise ValueError("duplicate item")


def _size(size: int, constraints: Mapping[str, Any], lower: str, upper: str) -> None:
    if lower in constraints and size < constraints[lower]:
        raise ValueError(lower)
    if upper in constraints and size > constraints[upper]:
        raise ValueError(upper)


def _fraction(value: int | float) -> Fraction:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite number")
    if isinstance(value, int) and value.bit_length() > 3400:
        raise ConstraintLimitError("numeric validation limit")
    text = str(value)
    if len(text) > 1024:
        raise ConstraintLimitError("numeric validation limit")
    return Fraction(text)


def _number_constraints(value: int | float, constraints: Mapping[str, Any]) -> None:
    number = _fraction(value)
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
        if key not in constraints:
            continue
        bound = _fraction(constraints[key])
        rejected = ((key == "minimum" and number < bound)
                    or (key == "maximum" and number > bound)
                    or (key == "exclusiveMinimum" and number <= bound)
                    or (key == "exclusiveMaximum" and number >= bound))
        if key == "multipleOf":
            rejected = bound <= 0 or (number / bound).denominator != 1
        if rejected:
            raise ValueError(key)


def _unique_key(value: Any, depth: int, budget: list[int]) -> Any:
    budget[0] -= 1
    if depth > 128 or budget[0] < 0:
        raise ConstraintLimitError("unique item nesting limit")
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True, exclude_unset=True)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("number", _fraction(value))
    if isinstance(value, Mapping):
        return ("object", tuple(sorted((key, _unique_key(item, depth + 1, budget)) for key, item in value.items())))
    if isinstance(value, (list, tuple)):
        return ("array", tuple(_unique_key(item, depth + 1, budget) for item in value))
    return ("scalar", value)


@lru_cache(maxsize=256)
def _pattern_adapter(pattern: str) -> TypeAdapter[str]:
    """Pydantic's Rust engine is linear-time; patterns are admitted at generation."""
    normalized: list[str] = []
    in_class = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            index += 1
            escaped, index = _pattern_escape(pattern, index, in_class)
            normalized.append(escaped)
        elif char == "." and not in_class:
            normalized.append("[^\\n\\r\u2028\u2029]")
        else:
            normalized.append(char)
            if char == "[":
                in_class = True
            elif char == "]":
                in_class = False
        index += 1
    return TypeAdapter(Annotated[str, StringConstraints(pattern="".join(normalized))])


_ECMA_SPACE = " \t\n\v\f\r\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


def _pattern_escape(pattern: str, index: int, in_class: bool) -> tuple[str, int]:
    escaped = pattern[index]
    replacement = {"d": "0-9", "D": "^0-9", "w": "A-Za-z0-9_", "W": "^A-Za-z0-9_", "s": _ECMA_SPACE, "S": "^" + _ECMA_SPACE}.get(escaped)
    if replacement is not None:
        return (replacement if in_class else f"[{replacement}]", index)
    if escaped in ("u", "x"):
        size = 4 if escaped == "u" else 2
        code = int(pattern[index + 1:index + size + 1], 16)
        index += size
        if 0xD800 <= code <= 0xDBFF and pattern[index + 1:index + 3] == "\\u":
            low = int(pattern[index + 3:index + 7], 16)
            code = 0x10000 + ((code - 0xD800) << 10) + low - 0xDC00
            index += 6
        text = chr(code)
        return (("\\" + text) if text in r"\.^$*+?{}[]()|-" else text, index)
    return "\\" + escaped, index
