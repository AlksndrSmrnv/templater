"""Server-side statusCode extraction for the «send» / «цепочка запросов» seam.

Mirrors ``app/static/js/status_code.js`` (``window.extractStatusCode``) so the
``status_code`` persisted in the send history matches the indicator the user saw
next to the send button: the first ``statusCode`` field (case-insensitive) found
at any nesting depth, current object's keys before descending. Only real numbers
and numeric strings count — booleans are NOT coerced. Returns the int or ``None``.
"""

from __future__ import annotations

import json
import re
from typing import Any

_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _coerce(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if value != value else int(value)  # NaN guard
    if isinstance(value, str) and _NUMERIC_RE.match(value.strip()):
        return int(float(value))
    return None


def _find(node: Any) -> int | None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.lower() == "statuscode":
                coerced = _coerce(value)
                if coerced is not None:
                    return coerced
        for value in node.values():
            found = _find(value)
            if found is not None:
                return found
        return None
    if isinstance(node, list):
        for item in node:
            found = _find(item)
            if found is not None:
                return found
    return None


def extract_status_code(body: str) -> int | None:
    """Parse ``body`` as JSON and pull out its ``statusCode`` (see module docs).

    ``None`` on parse error or when the field is absent / non-numeric.
    """

    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return _find(obj)
