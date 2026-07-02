from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import DATA_ENTITY_TYPES
from app.llm.prompts import (
    PROMPT_DEFS,
    load_prompt_overrides,
    prompt_setting_key,
    validate_prompt_text,
)
from app.repositories.settings import SettingsRepository
from app.routes.deps import SessionDep, TemplatesDep
from app.routes.htmx_utils import (
    form_bool,
    form_errors_response,
    form_str,
    toast_header,
    validation_errors_response,
)
from app.routes.uow import commit_and_refresh, commit_or_409
from app.schemas.access_group import AccessGroupCreate, AccessGroupUpdate
from app.schemas.attribute import (
    ALLOWED_TYPES,
    AttributeDefinitionCreate,
    AttributeDefinitionUpdate,
    AttributeReorder,
)
from app.schemas.header_preset import HeaderPresetCreate, HeaderPresetUpdate
from app.schemas.project import ProjectCreate, ProjectUpdate
from app.services.access_groups import AccessGroupService
from app.services.attribute_schema import AttributeSchemaService
from app.services.dynamic_patterns import (
    DYNAMIC_PATTERN_FIELDS,
    dynamic_pattern_fields,
    load_dynamic_patterns,
    save_dynamic_patterns,
)
from app.services.header_presets import HeaderPresetService
from app.services.projects import ProjectService
from app.utils.edit_mode import (
    COOKIE_NAME,
    TOKEN_TTL_SECONDS,
    check_edit_key,
    is_edit_mode,
    issue_edit_token,
)
from app.utils.errors import DomainError, NotFoundError, SettingsLockedError

router = APIRouter()


def require_edit_mode(request: Request) -> None:
    """Server-side gate for every settings mutation: hiding buttons in the
    template is cosmetic, a hand-crafted request must fail too."""

    if not is_edit_mode(request):
        raise SettingsLockedError("Настройки доступны только для просмотра")


# Everything that changes settings (or serves the edit forms) lives behind the
# edit-mode cookie; read-only views stay on the open ``router``.
edit_router = APIRouter(dependencies=[Depends(require_edit_mode)])


@router.post("/settings-htmx/unlock")
async def htmx_settings_unlock(request: Request) -> Response:
    form = await request.form()
    if not check_edit_key(form_str(form, "key")):
        return Response(
            status_code=200,
            headers={"HX-Trigger": toast_header("Неверный ключ", toast_type="error")},
        )
    response = Response(status_code=204, headers={"HX-Refresh": "true"})
    response.set_cookie(
        COOKIE_NAME,
        issue_edit_token(),
        max_age=TOKEN_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/templater",
    )
    return response


@router.post("/settings-htmx/lock")
async def htmx_settings_lock() -> Response:
    response = Response(status_code=204, headers={"HX-Refresh": "true"})
    response.delete_cookie(COOKIE_NAME, path="/templater")
    return response


@router.get("/settings")
async def page_settings(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    s = get_settings()
    saved_policy = await SettingsRepository(session).get("import_policy", "skip")
    default_policy = saved_policy if saved_policy in {"skip", "overwrite", "fail"} else "skip"
    dynamic_patterns = await load_dynamic_patterns(session)
    svc = AttributeSchemaService(session)
    attributes = await svc.list_all()
    usage_counts = await svc.usage(attributes)
    entity_types = list(DATA_ENTITY_TYPES)
    overrides = await load_prompt_overrides(session)
    prompts = [
        {
            "key": definition.key,
            "title": definition.title,
            "description": definition.description,
            "variables": definition.variables,
            "text": overrides.get(definition.key) or definition.default,
        }
        for definition in PROMPT_DEFS.values()
    ]
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active": "settings",
            "llm_active": s.llm_active,
            "llm_model": s.gigachat_model,
            "entity_types": entity_types,
            "attributes": attributes,
            "usage_counts": usage_counts,
            "selected_entity_type": "",
            "default_policy": default_policy,
            "dynamic_patterns": dynamic_patterns,
            "dynamic_pattern_fields": dynamic_pattern_fields(),
            "projects": await ProjectService(session).list_all(),
            "groups": await AccessGroupService(session).list_all(),
            "header_presets": await HeaderPresetService(session).list_all(),
            "prompts": prompts,
            "edit_mode": is_edit_mode(request),
            "unlock_available": bool(s.settings_edit_key),
        },
    )


def _attribute_options(form: Any) -> dict[str, Any]:
    data_type = form_str(form, "data_type")
    if data_type == "enum":
        values = [value.strip() for value in form_str(form, "enum_values").split(",") if value.strip()]
        return {"values": values}
    return {}


async def _attribute_create_payload(request: Request) -> AttributeDefinitionCreate:
    form = await request.form()
    data_type = form_str(form, "data_type")
    return AttributeDefinitionCreate(
        entity_type=form_str(form, "entity_type"),
        name=form_str(form, "name"),
        label=form_str(form, "label"),
        data_type=data_type,
        is_required=form_bool(form, "is_required"),
        display_order=int(form_str(form, "display_order") or 0),
        description=form_str(form, "description"),
        options=_attribute_options(form),
    )


async def _attribute_update_payload(request: Request, current_data_type: str) -> AttributeDefinitionUpdate:
    form = await request.form()
    display_order = int(form_str(form, "display_order") or 0)
    data_type = current_data_type
    options: dict[str, Any]
    if data_type == "enum":
        values = [value.strip() for value in form_str(form, "enum_values").split(",") if value.strip()]
        options = {"values": values}
    else:
        options = {}
    return AttributeDefinitionUpdate(
        label=form_str(form, "label"),
        is_required=form_bool(form, "is_required"),
        display_order=display_order,
        description=form_str(form, "description"),
        options=options,
    )


@router.get("/settings-htmx/attributes/table")
async def htmx_attributes_table(
    request: Request,
    entity_type: str = "",
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    svc = AttributeSchemaService(session)
    items = await svc.list_all()
    if entity_type:
        items = [item for item in items if item.entity_type == entity_type]
    usage_counts = await svc.usage(items)
    return templates.TemplateResponse(
        request,
        "partials/attributes_table.html",
        {"attributes": items, "usage_counts": usage_counts, "edit_mode": is_edit_mode(request)},
    )


@edit_router.get("/settings-htmx/attributes/new")
async def htmx_attribute_new(
    request: Request,
    entity_type: str = "",
    templates: Jinja2Templates = TemplatesDep,
) -> Response:
    return templates.TemplateResponse(
        request,
        "partials/attribute_form.html",
        {
            "attribute": None,
            "entity_types": list(DATA_ENTITY_TYPES),
            "data_types": sorted(ALLOWED_TYPES),
            "selected_entity_type": entity_type,
        },
    )


@edit_router.get("/settings-htmx/attributes/{attr_id}/edit")
async def htmx_attribute_edit(
    attr_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    svc = AttributeSchemaService(session)
    try:
        attribute = await svc.get(attr_id)
    except DomainError as exc:
        return form_errors_response(
            request,
            templates,
            exc.message,
            details=exc.details,
            status_code=exc.status_code,
        )
    return templates.TemplateResponse(
        request,
        "partials/attribute_form.html",
        {
            "attribute": attribute,
            "entity_types": list(DATA_ENTITY_TYPES),
            "data_types": sorted(ALLOWED_TYPES),
            "selected_entity_type": attribute.entity_type,
        },
    )


@edit_router.post("/settings-htmx/attributes")
async def htmx_attribute_create(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    svc = AttributeSchemaService(session)
    try:
        data = await _attribute_create_payload(request)
        await commit_and_refresh(session, await svc.create(data))
    except ValueError:
        return form_errors_response(request, templates, "Порядок должен быть числом")
    except DomainError as exc:
        return form_errors_response(
            request, templates, exc.message, details=exc.details, status_code=exc.status_code
        )
    except ValidationError as exc:
        return validation_errors_response(request, templates, exc)
    return Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Атрибут сохранён", close_modal=True, refresh_attributes=True)},
    )


@edit_router.put("/settings-htmx/attributes/{attr_id}")
async def htmx_attribute_update(
    attr_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    svc = AttributeSchemaService(session)
    try:
        attribute = await svc.get(attr_id)
        data = await _attribute_update_payload(request, attribute.data_type)
        await commit_and_refresh(session, await svc.update(attr_id, data))
    except ValueError:
        return form_errors_response(request, templates, "Порядок должен быть числом")
    except DomainError as exc:
        return form_errors_response(
            request, templates, exc.message, details=exc.details, status_code=exc.status_code
        )
    return Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Атрибут сохранён", close_modal=True, refresh_attributes=True)},
    )


@edit_router.delete("/settings-htmx/attributes/{attr_id}")
async def htmx_attribute_delete(
    attr_id: uuid.UUID, session: AsyncSession = SessionDep
) -> Response:
    svc = AttributeSchemaService(session)
    try:
        await svc.delete(attr_id)
    except DomainError as exc:
        return Response(
            status_code=exc.status_code,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
        )
    await commit_or_409(session, message="Не удалось удалить атрибут")
    return Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Атрибут удалён", refresh_attributes=True)},
    )


@edit_router.post("/settings-htmx/attributes/reorder")
async def htmx_attribute_reorder(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    form = await request.form()
    raw_order = [item for item in form_str(form, "order").split(",") if item.strip()]
    try:
        data = AttributeReorder(entity_type=form_str(form, "entity_type"), order=raw_order)
    except ValidationError:
        return form_errors_response(request, templates, "Некорректный список атрибутов")
    svc = AttributeSchemaService(session)
    try:
        await svc.reorder(data.entity_type, data.order)
    except DomainError as exc:
        return Response(
            status_code=exc.status_code,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
        )
    await commit_or_409(session, message="Не удалось изменить порядок атрибутов")
    return Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Порядок обновлён", refresh_attributes=True)},
    )


@router.get("/settings-htmx/projects/table")
async def htmx_projects_table(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    return templates.TemplateResponse(
        request,
        "partials/projects_table.html",
        {
            "projects": await ProjectService(session).list_all(),
            "edit_mode": is_edit_mode(request),
        },
    )


# The project forms submit with hx-swap="none", so failures must surface as
# toasts. Status 200 (not 4xx) so htmx processes the HX-Trigger header — on
# error statuses it ignores the response (see test_uow_and_errors.py).
_PROJECT_FORM_ERROR = "Проверьте название (1–255 символов) и цвет (#RRGGBB)"


def _project_error_response(message: str) -> Response:
    return Response(
        status_code=200,
        headers={"HX-Trigger": toast_header(message, toast_type="error")},
    )


@edit_router.post("/settings-htmx/projects")
async def htmx_project_create(
    request: Request,
    session: AsyncSession = SessionDep,
) -> Response:
    form = await request.form()
    svc = ProjectService(session)
    try:
        data = ProjectCreate(name=form_str(form, "name"), color=form_str(form, "color"))
        await commit_and_refresh(session, await svc.create(data))
    except ValidationError:
        return _project_error_response(_PROJECT_FORM_ERROR)
    except DomainError as exc:
        return _project_error_response(exc.message)
    return Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Проект сохранён", refresh_projects=True)},
    )


@edit_router.put("/settings-htmx/projects/{project_id}")
async def htmx_project_update(
    project_id: uuid.UUID,
    request: Request,
    session: AsyncSession = SessionDep,
) -> Response:
    form = await request.form()
    svc = ProjectService(session)
    try:
        data = ProjectUpdate(name=form_str(form, "name"), color=form_str(form, "color"))
        await commit_and_refresh(session, await svc.update(project_id, data))
    except ValidationError:
        return _project_error_response(_PROJECT_FORM_ERROR)
    except DomainError as exc:
        return _project_error_response(exc.message)
    return Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Проект сохранён", refresh_projects=True)},
    )


@edit_router.delete("/settings-htmx/projects/{project_id}")
async def htmx_project_delete(
    project_id: uuid.UUID, session: AsyncSession = SessionDep
) -> Response:
    svc = ProjectService(session)
    try:
        await svc.delete(project_id)
    except DomainError as exc:
        return _project_error_response(exc.message)
    await commit_or_409(session, message="Не удалось удалить проект")
    return Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Проект удалён", refresh_projects=True)},
    )


@router.get("/settings-htmx/groups/table")
async def htmx_groups_table(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    return templates.TemplateResponse(
        request,
        "partials/groups_table.html",
        {
            "groups": await AccessGroupService(session).list_all(),
            "edit_mode": is_edit_mode(request),
        },
    )


# Like the project forms, the group forms submit with hx-swap="none", so
# failures must surface as toasts on status 200 (htmx ignores HX-Trigger on 4xx).
_GROUP_FORM_ERROR = "Проверьте поля: название, цвет (#RRGGBB) и пароль"


def _group_error_response(message: str) -> Response:
    return Response(
        status_code=200,
        headers={"HX-Trigger": toast_header(message, toast_type="error")},
    )


@edit_router.post("/settings-htmx/groups")
async def htmx_group_create(
    request: Request,
    session: AsyncSession = SessionDep,
) -> Response:
    form = await request.form()
    svc = AccessGroupService(session)
    try:
        data = AccessGroupCreate(
            name=form_str(form, "name"),
            color=form_str(form, "color"),
            password=form_str(form, "password"),
        )
        await commit_and_refresh(session, await svc.create(data))
    except ValidationError:
        return _group_error_response(_GROUP_FORM_ERROR)
    except DomainError as exc:
        return _group_error_response(exc.message)
    return Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Группа создана", refresh_groups=True)},
    )


@edit_router.put("/settings-htmx/groups/{group_id}")
async def htmx_group_update(
    group_id: uuid.UUID,
    request: Request,
    session: AsyncSession = SessionDep,
) -> Response:
    form = await request.form()
    svc = AccessGroupService(session)
    try:
        # A blank password field means "leave the password unchanged".
        data = AccessGroupUpdate(
            name=form_str(form, "name"),
            color=form_str(form, "color"),
            password=form_str(form, "password") or None,
        )
        await commit_and_refresh(session, await svc.update(group_id, data))
    except ValidationError:
        return _group_error_response(_GROUP_FORM_ERROR)
    except DomainError as exc:
        return _group_error_response(exc.message)
    return Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Группа сохранена", refresh_groups=True)},
    )


@edit_router.delete("/settings-htmx/groups/{group_id}")
async def htmx_group_delete(
    group_id: uuid.UUID, session: AsyncSession = SessionDep
) -> Response:
    svc = AccessGroupService(session)
    try:
        await svc.delete(group_id)
    except DomainError as exc:
        return _group_error_response(exc.message)
    await commit_or_409(session, message="Не удалось удалить группу")
    return Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Группа удалена", refresh_groups=True)},
    )


async def _header_presets_context(request: Request, session: AsyncSession) -> dict[str, Any]:
    return {
        "header_presets": await HeaderPresetService(session).list_all(),
        "projects": await ProjectService(session).list_all(),
        "edit_mode": is_edit_mode(request),
    }


@router.get("/settings-htmx/header-presets/table")
async def htmx_header_presets_table(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    return templates.TemplateResponse(
        request,
        "partials/header_presets_table.html",
        await _header_presets_context(request, session),
    )


# Like the project forms, the preset forms submit with hx-swap="none", so
# failures must surface as toasts on status 200 (htmx ignores HX-Trigger on 4xx).
_PRESET_FORM_ERROR = "Проверьте поля пресета: название, проект и заголовки"


def _preset_error_response(message: str) -> Response:
    return Response(
        status_code=200,
        headers={"HX-Trigger": toast_header(message, toast_type="error")},
    )


def _preset_headers_from_form(form: Any) -> list[dict[str, Any]]:
    """Read the editor's hidden ``headers`` field (JSON array of {key,value})."""

    raw = form_str(form, "headers").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Некорректный JSON заголовков") from exc
    if not isinstance(parsed, list):
        raise ValueError("Заголовки должны быть списком")
    return parsed


@edit_router.post("/settings-htmx/header-presets")
async def htmx_header_preset_create(
    request: Request,
    session: AsyncSession = SessionDep,
) -> Response:
    form = await request.form()
    svc = HeaderPresetService(session)
    try:
        data = HeaderPresetCreate(
            name=form_str(form, "name"),
            project_id=form_str(form, "project_id"),
            url=form_str(form, "url"),
            headers=_preset_headers_from_form(form),
        )
        await commit_and_refresh(session, await svc.create(data))
    except (ValidationError, ValueError):
        return _preset_error_response(_PRESET_FORM_ERROR)
    except DomainError as exc:
        return _preset_error_response(exc.message)
    return Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Пресет сохранён", refresh_header_presets=True)},
    )


@edit_router.put("/settings-htmx/header-presets/{preset_id}")
async def htmx_header_preset_update(
    preset_id: uuid.UUID,
    request: Request,
    session: AsyncSession = SessionDep,
) -> Response:
    form = await request.form()
    svc = HeaderPresetService(session)
    try:
        data = HeaderPresetUpdate(
            name=form_str(form, "name"),
            project_id=form_str(form, "project_id"),
            url=form_str(form, "url"),
            headers=_preset_headers_from_form(form),
        )
        await commit_and_refresh(session, await svc.update(preset_id, data))
    except (ValidationError, ValueError):
        return _preset_error_response(_PRESET_FORM_ERROR)
    except DomainError as exc:
        return _preset_error_response(exc.message)
    return Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Пресет сохранён", refresh_header_presets=True)},
    )


@edit_router.delete("/settings-htmx/header-presets/{preset_id}")
async def htmx_header_preset_delete(
    preset_id: uuid.UUID, session: AsyncSession = SessionDep
) -> Response:
    svc = HeaderPresetService(session)
    try:
        await svc.delete(preset_id)
    except DomainError as exc:
        return _preset_error_response(exc.message)
    await commit_or_409(session, message="Не удалось удалить пресет")
    return Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Пресет удалён", refresh_header_presets=True)},
    )


@edit_router.put("/settings-htmx/import_policy")
async def htmx_import_policy(request: Request, session: AsyncSession = SessionDep) -> Response:
    form = await request.form()
    policy = form_str(form, "import_policy")
    if policy not in {"skip", "overwrite", "fail"}:
        policy = "skip"
    await SettingsRepository(session).set("import_policy", policy)
    await commit_or_409(session)
    return Response(status_code=204, headers={"HX-Redirect": "/templater/settings?saved=1"})


@edit_router.put("/settings-htmx/dynamic-fields")
async def htmx_dynamic_fields(request: Request, session: AsyncSession = SessionDep) -> Response:
    """Persist the generation patterns for the dynamic envelope fields.

    Each field is submitted as ``pattern_<token>``; blanks fall back to that
    field's default in :func:`normalize_dynamic_patterns`, so the stored map
    always covers every field.
    """

    form = await request.form()
    raw = {
        field["name"]: form_str(form, f"pattern_{field['name']}")
        for field in DYNAMIC_PATTERN_FIELDS
    }
    await save_dynamic_patterns(session, raw)
    await commit_or_409(session)
    return Response(status_code=204, headers={"HX-Redirect": "/templater/settings?saved=1"})


@edit_router.put("/settings-htmx/prompts/{key}")
async def htmx_prompt(key: str, request: Request, session: AsyncSession = SessionDep) -> Response:
    """Persist an edited LLM system instruction.

    A blank value clears the override, so the next run falls back to the coded
    default. The change is read fresh on the next ``llm_service`` call, so it
    applies to the very next analysis run without a restart.
    """

    if key not in PROMPT_DEFS:
        raise NotFoundError("Неизвестный промпт")
    form = await request.form()
    text = form_str(form, "text").strip()
    try:
        validate_prompt_text(key, text)
    except DomainError as exc:
        return Response(
            status_code=200,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
        )
    await SettingsRepository(session).set(prompt_setting_key(key), text)
    await commit_or_409(session)
    return Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Промпт сохранён")},
    )
