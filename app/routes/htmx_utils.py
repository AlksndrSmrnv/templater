from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.datastructures import FormData

from app.db.models import AttributeDefinition


def form_str(form: FormData, key: str) -> str:
    value = form.get(key)
    return value if isinstance(value, str) else ""


def form_bool(form: FormData, key: str) -> bool:
    return form_str(form, key).lower() in {"1", "true", "yes", "on"}


def parse_json_path(raw: str) -> list[str]:
    """Decode a folder path sent as a JSON array of segments (empty/absent ⇒ root)."""

    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return [str(seg).strip() for seg in value if str(seg).strip()]


def parse_uuid_list(raw: str) -> list[uuid.UUID]:
    """Decode a comma-separated UUID list (drag-and-drop ``order`` payloads)."""

    out: list[uuid.UUID] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            out.append(uuid.UUID(item))
        except ValueError:
            continue
    return out


def parse_reorder_payload(raw: str) -> list[tuple[str, uuid.UUID]]:
    """Decode the unified drag-and-drop ``order`` payload: a comma-separated
    list of ``<kind>:<uuid>`` tokens where ``kind`` is ``t`` (filled template)
    or ``c`` (request chain). Unknown kinds and malformed UUIDs are dropped —
    the service ignores ids that don't belong to the target folder anyway, so a
    crafted/stale token can't corrupt the renumber."""

    out: list[tuple[str, uuid.UUID]] = []
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        kind, _, raw_id = token.partition(":")
        kind = kind.strip().lower()
        if kind not in ("t", "c"):
            continue
        try:
            out.append((kind, uuid.UUID(raw_id.strip())))
        except ValueError:
            continue
    return out


def read_entity_attributes(form: FormData, schema: list[AttributeDefinition]) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for field in schema:
        key = f"attr_{field.name}"
        if field.data_type == "bool":
            attrs[field.name] = form_bool(form, key)
            continue
        value = form_str(form, key)
        if value != "":
            attrs[field.name] = value
    return attrs


def toast_header(message: str, *, toast_type: str = "success", **events: Any) -> str:
    normalized_events = {key.replace("_", "-"): value for key, value in events.items()}
    payload: dict[str, Any] = {
        "showToast": {"message": message, "type": toast_type},
        **normalized_events,
    }
    # ensure_ascii=True (default) escapes non-ASCII as \uXXXX so the value is
    # latin-1-safe — HTTP header values are encoded as latin-1 by Starlette, and
    # raw Cyrillic here would raise UnicodeEncodeError → 500 (no htmx swap/toast).
    # htmx unescapes it back to the original text via JSON.parse on the client.
    return json.dumps(payload)


def form_errors_response(
    request: Request,
    templates: Jinja2Templates,
    message: str,
    *,
    details: Any | None = None,
    status_code: int = 422,
    headers: dict[str, str] | None = None,
) -> Response:
    errors = details if isinstance(details, list) else []
    return templates.TemplateResponse(
        request,
        "partials/form_errors.html",
        {"message": message, "errors": errors},
        status_code=status_code,
        headers=headers,
    )


def validation_errors_response(
    request: Request,
    templates: Jinja2Templates,
    exc: ValidationError,
    *,
    status_code: int = 422,
    headers: dict[str, str] | None = None,
) -> Response:
    return form_errors_response(
        request,
        templates,
        "Проверьте поля формы",
        details=[str(error["msg"]) for error in exc.errors()],
        status_code=status_code,
        headers=headers,
    )
