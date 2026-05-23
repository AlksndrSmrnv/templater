"""Shared helpers for template path parsing and tokenization.

The app sees three closely related path forms: JSON Pointer / XML-ish leaf paths
from walkers, dotted catalog suggestions from the LLM, and XML attribute/index
markers. Use ``pointer_path_segments`` when dots are literal key characters.
"""

from __future__ import annotations

import re

CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-zа-яё0-9])(?=[A-ZА-ЯЁ])")
ACRONYM_BOUNDARY_RE = re.compile(r"(?<=[A-ZА-ЯЁ])(?=[A-ZА-ЯЁ][a-zа-яё])")
NON_TOKEN_CHARS_RE = re.compile(r"[_\W]+", flags=re.UNICODE)
# Template paths are JSON pointers / XML-ish paths, where bracketed chunks are indexes.
PATH_INDEX_RE = re.compile(r"\[[^\]]*\]")


def path_segments(path: str) -> list[str]:
    if not path:
        return []
    raw_segments = re.split(r"[/.]+", path.strip("/."))
    return _clean_segments(raw_segments)


def pointer_path_segments(path: str) -> list[str]:
    if not path:
        return []
    return _clean_segments(path.strip("/").split("/"))


def _clean_segments(raw_segments: list[str]) -> list[str]:
    out: list[str] = []
    for raw in raw_segments:
        segment = clean_path_segment(raw)
        if segment is not None:
            out.append(segment)
    return out


def clean_path_segment(segment: str) -> str | None:
    segment = segment.strip()
    if not segment or segment == "#text":
        return None
    segment = PATH_INDEX_RE.sub("", segment)
    if segment.startswith("@"):
        segment = segment[1:]
    segment = segment.replace("~1", "/").replace("~0", "~")
    if not segment or segment.isdigit():
        return None
    return segment


def segment_tokens(segment: str) -> set[str]:
    spaced = CAMEL_BOUNDARY_RE.sub(" ", segment)
    spaced = ACRONYM_BOUNDARY_RE.sub(" ", spaced)
    spaced = NON_TOKEN_CHARS_RE.sub(" ", spaced)
    return {token.lower() for token in spaced.split() if token}
