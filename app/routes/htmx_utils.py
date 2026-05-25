from __future__ import annotations

import json
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
    return json.dumps(payload, ensure_ascii=False)


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
