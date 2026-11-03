"""A tool call's inputs against the parameters its tool declares: checked once, for every caller.

The planner's direct calls sent `{"order_id": 4471}` to a tool declaring a string
`order_id`, and once `{"id": 9999}` for it: a strict server rejects both, and the
call fell back to an agent (issue #76, W5). A value is converted only where nothing
it means can change (4471 to "4471", "120" to 120); anything else is reported.
"""

from __future__ import annotations

import re
from typing import Any

_INTEGER_TEXT = re.compile(r"-?\d+")
_NUMBER_TEXT = re.compile(r"-?\d+(\.\d+)?")


def conform_params(schema: dict[str, Any], params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """*params* as *schema* declares them, and what does not fit ([] when all of it does).

    Parameters the schema does not name (north's own task_id and workspace among
    them) pass as they are, and so does a null for an optional one.
    """
    properties: dict[str, Any] = schema.get("properties") or {}
    problems = [f"missing {name!r}" for name in schema.get("required") or () if params.get(name) is None]
    conformed = dict(params)
    for name, value in params.items():
        declared = properties.get(name, {}).get("type")
        if value is None or declared is None:
            continue
        types = declared if isinstance(declared, list) else [declared]
        if (converted := _as_any(value, types)) is _MISMATCH:
            problems.append(f"{name!r} must be {' or '.join(types)}, not {value!r}")
        else:
            conformed[name] = converted
    return conformed, problems


_MISMATCH = object()


def _as_any(value: Any, types: list[str]) -> Any:
    """*value* as the first of *types* it fits, or _MISMATCH."""
    for expected in types:
        fits, converted = _as(value, expected)
        if fits:
            return converted
    return _MISMATCH


def _as(value: Any, expected: str) -> tuple[bool, Any]:
    """*value* as the JSON type *expected*, when that changes nothing it means."""
    number = isinstance(value, int | float) and not isinstance(value, bool)
    text = value.strip() if isinstance(value, str) else None
    if expected == "string":
        return (True, value) if isinstance(value, str) else (number, str(value) if number else value)
    if expected == "integer":
        if number and float(value).is_integer():
            return True, int(value)
        return (True, int(text)) if text is not None and _INTEGER_TEXT.fullmatch(text) else (False, value)
    if expected == "number":
        if number:
            return True, value
        if text is not None and _NUMBER_TEXT.fullmatch(text):
            return True, float(text) if "." in text else int(text)
        return False, value
    if expected == "boolean":
        if isinstance(value, bool):
            return True, value
        return (True, text == "true") if text in ("true", "false") else (False, value)
    if expected == "array":
        return isinstance(value, list), value
    if expected == "object":
        return isinstance(value, dict), value
    return True, value  # a type this check does not know is left to the tool
