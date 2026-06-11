"""Common parse result shared by all collection importers.

A parser converts a tool-specific document (Postman, Insomnia, …) into these
plain dataclasses; the persistence layer then maps each :class:`ParsedRequest`
onto a ``MessageTemplate`` row without knowing the source format.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET


@dataclass
class ParsedRequest:
    """One request extracted from a collection, ready to become a template."""

    name: str
    description: str = ""
    folder_path: list[str] = field(default_factory=list)
    http_method: str = ""
    url: str = ""
    # List of {"key","value","mode","original","disabled"} header dicts.
    headers: list[dict[str, str | bool]] = field(default_factory=list)
    content: str = ""
    fmt: str = "json"  # 'json' | 'xml'
    # ``False`` when ``content`` does not parse as ``fmt`` (e.g. GET with no
    # body, urlencoded/formdata/GraphQL bodies). Such templates are still
    # imported, but LLM analysis is skipped for them.
    parsable: bool = False


@dataclass
class ParsedCollection:
    """A parsed collection plus the flat list of its requests."""

    name: str
    description: str = ""
    source: str = "postman"
    source_format: str = ""
    variables: list[dict[str, object]] = field(default_factory=list)
    requests: list[ParsedRequest] = field(default_factory=list)


def make_header(key: str, value: str, *, disabled: bool) -> dict[str, str | bool]:
    """Build the normalised header dict shared by all importers; ``original``
    keeps the raw value so dynamic-token detection can rewrite ``value``
    without losing the source."""

    return {"key": key, "value": value, "mode": "literal", "original": value, "disabled": disabled}


def detect_format(content: str, language: str) -> tuple[str, str, bool]:
    """Pick ``json``/``xml`` from the language hint + a content sniff.

    Returns ``(content, fmt, parsable)`` where ``parsable`` is ``True`` only if
    the content actually parses as the chosen format.
    """

    stripped = content.lstrip()
    prefers_xml = language == "xml" or (not language and stripped.startswith("<"))
    if prefers_xml:
        try:
            ET.fromstring(content)
            return content, "xml", True
        except ET.ParseError:
            return content, "xml", False
    # default to JSON
    try:
        json.loads(content)
        return content, "json", True
    except (json.JSONDecodeError, ValueError):
        # JSON hint but invalid, or unknown language with non-XML content.
        if stripped.startswith("<"):
            try:
                ET.fromstring(content)
                return content, "xml", True
            except ET.ParseError:
                return content, "xml", False
        return content, "json", False
