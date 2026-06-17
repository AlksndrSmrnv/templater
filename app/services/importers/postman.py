"""Parse a Postman Collection v2.1 (and the close-enough v2.0) document.

Pure, DB-free function: takes the already-decoded JSON ``dict`` and returns a
:class:`~app.services.importers.base.ParsedCollection`. Validation mirrors the
strict-but-readable style of :mod:`app.services.export_import` — a malformed
top-level shape raises :class:`ValidationFailed`, while individual items degrade
gracefully (a request that can't be understood is skipped, an unparsable body is
kept verbatim with ``parsable=False``).
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


def parse_postman_collection(data: Any) -> ParsedCollection:
    """Convert a decoded Postman collection JSON into a ``ParsedCollection``."""

    if not isinstance(data, dict):
        raise ValidationFailed("Файл коллекции должен быть JSON-объектом")
    info = data.get("info")
    if not isinstance(info, dict):
        raise ValidationFailed("В коллекции отсутствует объект info")
    items = data.get("item")
    if not isinstance(items, list):
        raise ValidationFailed("В коллекции отсутствует список item")

    name = _as_str(info.get("name")) or "Импортированная коллекция"
    description = _as_description(info.get("description"))
    source_format = _detect_schema_version(info.get("schema"))

    requests: list[ParsedRequest] = []
    for entry in items:
        _walk_item(entry, [], requests)

    return ParsedCollection(
        name=name,
        description=description,
        source="postman",
        source_format=source_format,
        variables=_as_variables(data.get("variable")),
        requests=requests,
    )


def _walk_item(entry: Any, folder_path: list[str], out: list[ParsedRequest]) -> None:
    """Recursively flatten the Postman item tree into ``out``.

    A node with a nested ``item`` list is a folder; one with a ``request`` is a
    leaf request. Anything else (empty folder, malformed node) is skipped.
    """

    if not isinstance(entry, dict):
        return
    item_name = _as_str(entry.get("name")) or UNNAMED
    nested = entry.get("item")
    # A node may carry its own ``request`` *and* a nested ``item`` list (rare, but
    # valid). Emit the request regardless of whether the node also acts as a folder
    # so it isn't silently dropped.
    if "request" in entry:
        out.append(_parse_request(entry, folder_path, item_name))
    if isinstance(nested, list):
        child_path = [*folder_path, item_name]
        for child in nested:
            _walk_item(child, child_path, out)


def _parse_request(entry: dict[str, Any], folder_path: list[str], name: str) -> ParsedRequest:
    request = entry.get("request")
    # A request may be a bare URL string in older exports.
    if isinstance(request, str):
        return ParsedRequest(
            name=name,
            description=_as_description(entry.get("description")),
            folder_path=list(folder_path),
            http_method="GET",
            url=request,
        )
    if not isinstance(request, dict):
        request = {}

    method = _as_str(request.get("method")).upper() or "GET"
    content, fmt, parsable = _parse_body(request.get("body"))
    return ParsedRequest(
        name=name,
        description=_as_description(request.get("description")) or _as_description(entry.get("description")),
        folder_path=list(folder_path),
        http_method=method,
        url=_parse_url(request.get("url")),
        headers=_parse_headers(request.get("header")),
        content=content,
        fmt=fmt,
        parsable=parsable,
    )


def _parse_url(url: Any) -> str:
    if isinstance(url, str):
        return url
    if isinstance(url, dict):
        raw = url.get("raw")
        if isinstance(raw, str):
            return raw
        host = url.get("host")
        path = url.get("path")
        host_str = ".".join(host) if isinstance(host, list) else _as_str(host)
        path_str = "/".join(str(p) for p in path) if isinstance(path, list) else _as_str(path)
        joined = "/".join(part for part in (host_str, path_str) if part)
        return joined
    return ""


def _parse_headers(header: Any) -> list[dict[str, str | bool]]:
    """Normalise Postman header entries to ``{key,value,mode,original,disabled}``.

    Postman stores headers as a list of objects (or, rarely, a raw string); each
    entry keeps its raw value as ``original`` so dynamic-token detection in the
    service layer can rewrite ``value`` without losing the source.
    """

    out: list[dict[str, str | bool]] = []
    if isinstance(header, str):
        for line in header.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if key:
                out.append(make_header(key, value, disabled=False))
        return out
    if not isinstance(header, list):
        return out
    for item in header:
        if not isinstance(item, dict):
            continue
        key = _as_str(item.get("key"))
        if not key:
            continue
        out.append(
            make_header(key, _as_str(item.get("value")), disabled=bool(item.get("disabled", False)))
        )
    return out


def _parse_body(body: Any) -> tuple[str, str, bool]:
    """Return ``(content, fmt, parsable)`` for a Postman request body.

    Only ``raw`` bodies carry template content. An empty or missing ``mode``
    (seen in not-quite-Postman exports) is treated as raw when ``raw`` is
    present. Other modes (urlencoded, formdata, file, graphql) and absent
    bodies yield empty, non-parsable content — the request is still imported
    for its headers/URL/method.
    """

    if not isinstance(body, dict):
        return "", "json", False
    # Some non-Postman exports leave ``mode`` empty (or omit it) while still
    # carrying the payload in ``raw``. Treat empty/missing mode as raw-eligible;
    # only an explicit non-raw mode (urlencoded/formdata/file/graphql) is skipped.
    mode = _as_str(body.get("mode")).lower()
    if mode not in ("", "raw"):
        return "", "json", False
    raw = body.get("raw")
    if not isinstance(raw, str) or not raw.strip():
        return "", "json", False
    language = _body_language(body)
    return detect_format(raw, language)


def _body_language(body: dict[str, Any]) -> str:
    options = body.get("options")
    if isinstance(options, dict):
        raw_opts = options.get("raw")
        if isinstance(raw_opts, dict):
            return _as_str(raw_opts.get("language")).lower()
    return ""


def _detect_schema_version(schema: Any) -> str:
    text = _as_str(schema)
    if not text:
        return "unknown"
    if "v2.1" in text:
        return "v2.1.0"
    if "v2.0" in text:
        return "v2.0.0"
    return text


def _as_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_description(value: Any) -> str:
    """Postman description may be a string or ``{"content": "...", ...}``."""

    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _as_str(value.get("content"))
    return ""


def _as_variables(value: Any) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
