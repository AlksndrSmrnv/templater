"""Common parse result shared by all collection importers.

A parser converts a tool-specific document (Postman, Insomnia, …) into these
plain dataclasses; the persistence layer then maps each :class:`ParsedRequest`
onto a ``MessageTemplate`` row without knowing the source format.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
