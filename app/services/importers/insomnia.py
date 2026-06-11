"""Parse an Insomnia Export Format v4 (JSON) document.

Pure, DB-free function: takes the already-decoded JSON ``dict`` and returns a
:class:`~app.services.importers.base.ParsedCollection`. The export is a flat
``resources`` list; the folder hierarchy is rebuilt from ``parentId`` links
(request → request_group* → workspace) and siblings are ordered by
``metaSortKey``, matching the order shown in Insomnia. A malformed top-level
shape raises :class:`ValidationFailed`, while individual resources degrade
gracefully (a broken ``parentId`` chain just shortens the folder path, an
unparsable body is kept verbatim with ``parsable=False``).
"""

from __future__ import annotations

from typing import Any

from app.services.importers.base import (
    ParsedCollection,
    ParsedRequest,
    detect_format,
    make_header,
)
from app.utils.errors import ValidationFailed

UNNAMED = "(без имени)"

_JSON_MIMES = {"application/json"}
_XML_MIMES = {"application/xml", "text/xml"}


def parse_insomnia_collection(data: Any) -> ParsedCollection:
    """Convert a decoded Insomnia v4 export JSON into a ``ParsedCollection``."""

    if not isinstance(data, dict):
        raise ValidationFailed("Файл коллекции должен быть JSON-объектом")
    resources = data.get("resources")
    if not isinstance(resources, list):
        raise ValidationFailed("В экспорте Insomnia отсутствует список resources")

    resources = [r for r in resources if isinstance(r, dict)]
    workspace = next((r for r in resources if r.get("_type") == "workspace"), None)
    name = _as_str((workspace or {}).get("name")) or "Импортированная коллекция"
    description = _as_str((workspace or {}).get("description"))

    export_format = data.get("__export_format")
    source_format = f"v{export_format}" if isinstance(export_format, (int, float)) else "unknown"

    groups = {r["_id"]: r for r in resources if r.get("_type") == "request_group" and "_id" in r}

    # Group requests and folders by parent and walk the tree depth-first with
    # siblings ordered by ``metaSortKey``, so the flat list matches the order
    # Insomnia shows. Roots are parent ids that aren't request groups (the
    # workspace, or a dangling id from a broken export).
    children: dict[Any, list[dict[str, Any]]] = {}
    for r in resources:
        if r.get("_type") in ("request", "request_group"):
            children.setdefault(r.get("parentId"), []).append(r)
    for siblings in children.values():
        siblings.sort(key=_meta_sort_key)

    requests: list[ParsedRequest] = []
    emitted: set[int] = set()
    root_ids = [pid for pid in children if pid not in groups]
    for root_id in root_ids:
        _walk(root_id, children, groups, set(), emitted, requests)
    # Requests unreachable from any root (a ``parentId`` cycle among groups)
    # are still imported, appended in source order.
    for r in resources:
        if r.get("_type") == "request" and id(r) not in emitted:
            requests.append(_parse_request(r, _folder_path(r, groups)))

    return ParsedCollection(
        name=name,
        description=description,
        source="insomnia",
        source_format=source_format,
        variables=_collect_variables(resources),
        requests=requests,
    )


def _walk(
    parent_id: Any,
    children: dict[Any, list[dict[str, Any]]],
    groups: dict[Any, dict[str, Any]],
    visited: set[Any],
    emitted: set[int],
    out: list[ParsedRequest],
) -> None:
    """Depth-first traversal emitting requests in display order; ``visited``
    guards against ``parentId`` cycles in a corrupted export."""

    if parent_id in visited:
        return
    visited.add(parent_id)
    for resource in children.get(parent_id, []):
        if resource.get("_type") == "request":
            emitted.add(id(resource))
            out.append(_parse_request(resource, _folder_path(resource, groups)))
        else:
            _walk(resource.get("_id"), children, groups, visited, emitted, out)


def _meta_sort_key(resource: dict[str, Any]) -> float:
    value = resource.get("metaSortKey")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _folder_path(resource: dict[str, Any], groups: dict[Any, dict[str, Any]]) -> list[str]:
    """Materialise the folder path by walking ``parentId`` links through
    ``request_group`` resources; stops at the workspace, a missing parent, or a
    cycle."""

    path: list[str] = []
    seen: set[Any] = set()
    parent_id = resource.get("parentId")
    while parent_id in groups and parent_id not in seen:
        seen.add(parent_id)
        group = groups[parent_id]
        path.append(_as_str(group.get("name")) or UNNAMED)
        parent_id = group.get("parentId")
    path.reverse()
    return path


def _parse_request(resource: dict[str, Any], folder_path: list[str]) -> ParsedRequest:
    method = _as_str(resource.get("method")).upper() or "GET"
    content, fmt, parsable = _parse_body(resource.get("body"))
    return ParsedRequest(
        name=_as_str(resource.get("name")) or UNNAMED,
        description=_as_str(resource.get("description")),
        folder_path=folder_path,
        http_method=method,
        url=_as_str(resource.get("url")),
        headers=_parse_headers(resource.get("headers")),
        content=content,
        fmt=fmt,
        parsable=parsable,
    )


def _parse_headers(headers: Any) -> list[dict[str, str | bool]]:
    """Insomnia stores headers as ``[{name, value, disabled}]`` objects."""

    out: list[dict[str, str | bool]] = []
    if not isinstance(headers, list):
        return out
    for item in headers:
        if not isinstance(item, dict):
            continue
        key = _as_str(item.get("name"))
        if not key:
            continue
        out.append(
            make_header(key, _as_str(item.get("value")), disabled=bool(item.get("disabled", False)))
        )
    return out


def _parse_body(body: Any) -> tuple[str, str, bool]:
    """Return ``(content, fmt, parsable)`` for an Insomnia request body.

    Only text bodies with a JSON/XML mime type (or no mime type at all, sniffed
    by content) carry template content. Other mime types (urlencoded, multipart,
    GraphQL, file) and absent bodies yield empty, non-parsable content — the
    request is still imported for its headers/URL/method.
    """

    if not isinstance(body, dict):
        return "", "json", False
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return "", "json", False
    mime = _as_str(body.get("mimeType")).lower().partition(";")[0]
    if mime in _JSON_MIMES:
        language = "json"
    elif mime in _XML_MIMES:
        language = "xml"
    elif mime:
        # urlencoded/multipart/graphql/file etc. — keep the request, skip the body.
        return "", "json", False
    else:
        language = ""
    return detect_format(text, language)


def _collect_variables(resources: list[dict[str, Any]]) -> list[dict[str, object]]:
    """Flatten all environment resources' ``data`` dicts into the Postman-like
    ``[{key, value}]`` shape stored on the collection."""

    out: list[dict[str, object]] = []
    seen: set[str] = set()
    for resource in resources:
        if resource.get("_type") != "environment":
            continue
        env_data = resource.get("data")
        if not isinstance(env_data, dict):
            continue
        for key, value in env_data.items():
            key = str(key)
            if key in seen:
                continue
            seen.add(key)
            out.append({"key": key, "value": value if isinstance(value, str) else str(value)})
    return out


def _as_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
