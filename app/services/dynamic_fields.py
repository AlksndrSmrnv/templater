from __future__ import annotations

import re

DYNAMIC_FIELD_TOKENS: dict[str, str] = {
    "rquid": "rqUID",
    "operuid": "operUID",
    "rqtm": "rqTm",
    "channeldatetime": "channelDateTime",
}
DYNAMIC_TOKENS: frozenset[str] = frozenset(DYNAMIC_FIELD_TOKENS.values())

# Stable, UI-ready ordering of envelope tokens. The placeholder dropdown shows
# them in this exact sequence; ``DYNAMIC_TOKENS`` is a frozenset and would
# render in non-deterministic order if used directly.
_DYNAMIC_TOKEN_CATALOG: tuple[dict[str, str], ...] = (
    {"name": "rqUID", "label": "ID запроса (rqUID)"},
    {"name": "operUID", "label": "ID операции (operUID)"},
    {"name": "rqTm", "label": "Время запроса (rqTm)"},
    {"name": "channelDateTime", "label": "Дата/время канала (channelDateTime)"},
)


def dynamic_token_catalog() -> list[dict[str, str]]:
    """Return UI catalog of dynamic envelope tokens for placeholder dropdown.

    Each entry: ``{"name": <canonical token>, "label": <human-readable>}``.
    Order is stable and matches the order users see in the picker.
    """

    return [dict(entry) for entry in _DYNAMIC_TOKEN_CATALOG]

_XML_INDEX_RE = re.compile(r"\[\d+\]$")
_INDEX_SEGMENT_RE = re.compile(r"\d+|\[\d+\]")
_NON_ALNUM_RE = re.compile(r"[^0-9A-Za-z]+")


def resolve_dynamic_token(location: str) -> str | None:
    """Resolve canonical dynamic token name from a JSON-pointer / XML path."""

    for segment in reversed(location.split("/")):
        name = _clean_segment(segment)
        if name is None:
            continue
        return DYNAMIC_FIELD_TOKENS.get(_normalize_name(name))
    return None


def _clean_segment(segment: str) -> str | None:
    segment = segment.strip()
    if not segment or segment == "#text":
        return None
    segment = _XML_INDEX_RE.sub("", segment)
    if not segment or _INDEX_SEGMENT_RE.fullmatch(segment):
        return None
    if segment.startswith("@"):
        segment = segment[1:]
    segment = _decode_json_pointer_segment(segment)
    return segment or None


def _normalize_name(name: str) -> str:
    return _NON_ALNUM_RE.sub("", name).lower()


def _decode_json_pointer_segment(segment: str) -> str:
    return segment.replace("~1", "/").replace("~0", "~")
