from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request
from fastapi.routing import APIRoute

import app.utils.edit_mode as edit_mode
from app.routes import settings as settings_routes
from app.utils.edit_mode import (
    COOKIE_NAME,
    check_edit_key,
    is_edit_mode,
    issue_edit_token,
    verify_edit_token,
)
from app.utils.errors import SettingsLockedError


class FakeFormRequest:
    def __init__(self, form: dict[str, str]) -> None:
        self._form = form

    async def form(self) -> dict[str, str]:
        return self._form


def request_with_cookies(cookies: dict[str, str]) -> Request:
    return cast(Request, SimpleNamespace(cookies=cookies))


@pytest.fixture()
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    cfg = SimpleNamespace(signing_key=b"k" * 32, settings_edit_key="sesame")
    monkeypatch.setattr(edit_mode, "get_settings", lambda: cfg)
    return cfg


def test_edit_token_roundtrip_and_expiry(fake_settings: SimpleNamespace) -> None:
    token = issue_edit_token()
    assert verify_edit_token(token) is True

    issued_in_the_past = issue_edit_token(now=time.time() - edit_mode.TOKEN_TTL_SECONDS - 10)
    assert verify_edit_token(issued_in_the_past) is False


def test_edit_token_rejects_garbage_and_tampering(fake_settings: SimpleNamespace) -> None:
    token = issue_edit_token()
    expires_str, _, signature = token.partition(".")

    assert verify_edit_token(None) is False
    assert verify_edit_token("") is False
    assert verify_edit_token("no-dot") is False
    assert verify_edit_token("notanumber." + signature) is False
    assert verify_edit_token(expires_str + ".deadbeef") is False
    # Extending the lifetime invalidates the signature.
    assert verify_edit_token(str(int(expires_str) + 3600) + "." + signature) is False


def test_edit_token_is_bound_to_signing_key(fake_settings: SimpleNamespace) -> None:
    token = issue_edit_token()
    fake_settings.signing_key = b"x" * 32
    assert verify_edit_token(token) is False


def test_check_edit_key(fake_settings: SimpleNamespace) -> None:
    assert check_edit_key("sesame") is True
    assert check_edit_key("wrong") is False
    assert check_edit_key("") is False

    # An unset key means unlocking is impossible, not that anything matches.
    fake_settings.settings_edit_key = ""
    assert check_edit_key("sesame") is False
    assert check_edit_key("") is False


def test_is_edit_mode_reads_cookie(fake_settings: SimpleNamespace) -> None:
    assert is_edit_mode(request_with_cookies({})) is False
    assert is_edit_mode(request_with_cookies({COOKIE_NAME: "junk"})) is False
    assert is_edit_mode(request_with_cookies({COOKIE_NAME: issue_edit_token()})) is True


def test_require_edit_mode_gate(fake_settings: SimpleNamespace) -> None:
    with pytest.raises(SettingsLockedError):
        settings_routes.require_edit_mode(request_with_cookies({}))

    settings_routes.require_edit_mode(
        request_with_cookies({COOKIE_NAME: issue_edit_token()})
    )


async def test_unlock_with_wrong_key_shows_toast_and_sets_no_cookie(
    fake_settings: SimpleNamespace,
) -> None:
    response = await settings_routes.htmx_settings_unlock(
        cast(Request, FakeFormRequest({"key": "wrong"}))
    )
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    payload = json.loads(response.headers["HX-Trigger"])
    assert payload["showToast"]["type"] == "error"


async def test_unlock_with_right_key_sets_valid_cookie(
    fake_settings: SimpleNamespace,
) -> None:
    response = await settings_routes.htmx_settings_unlock(
        cast(Request, FakeFormRequest({"key": "sesame"}))
    )
    assert response.status_code == 204
    assert response.headers["HX-Refresh"] == "true"
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{COOKIE_NAME}=")
    assert "HttpOnly" in set_cookie
    token = set_cookie.split(";", 1)[0].split("=", 1)[1].strip('"')
    assert verify_edit_token(token) is True


async def test_lock_clears_cookie(fake_settings: SimpleNamespace) -> None:
    response = await settings_routes.htmx_settings_lock()
    assert response.status_code == 204
    assert response.headers["HX-Refresh"] == "true"
    assert response.headers["set-cookie"].startswith(f"{COOKIE_NAME}=")


class FakeUploadFile:
    def __init__(self, payload: dict[str, object]) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    async def read(self) -> bytes:
        return self._raw


class FakeTemplateRenderer:
    def TemplateResponse(
        self,
        request: object,
        name: str,
        context: dict[str, object],
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            request=request,
            name=name,
            context=context,
            status_code=status_code,
            headers=headers or {},
        )


@pytest.fixture()
def import_service_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    from app.routes import export_import as export_import_routes

    calls: list[dict[str, object]] = []

    class FakeService:
        def __init__(self, session: object) -> None:
            pass

        async def import_package(self, package: dict[str, object], *, policy: str) -> SimpleNamespace:
            calls.append(package)
            return SimpleNamespace(model_dump=lambda: {})

    class FakeSettingsRepo:
        def __init__(self, session: object) -> None:
            pass

        async def get(self, key: str, default: str) -> str:
            return default

    monkeypatch.setattr(export_import_routes, "ExportImportService", FakeService)
    monkeypatch.setattr(export_import_routes, "SettingsRepository", FakeSettingsRepo)
    return calls


async def run_import(package: dict[str, object], cookies: dict[str, str]) -> SimpleNamespace:
    from app.routes.export_import import htmx_import

    return cast(
        SimpleNamespace,
        await htmx_import(
            request_with_cookies(cookies),
            file=cast(Any, FakeUploadFile(package)),
            policy="overwrite",
            templates=cast(Any, FakeTemplateRenderer()),
            session=cast(Any, None),
        ),
    )


async def test_import_with_attribute_schema_is_blocked_when_locked(
    fake_settings: SimpleNamespace, import_service_spy: list[dict[str, object]]
) -> None:
    package = {"attribute_schema": [{"entity_type": "client", "name": "x"}], "clients": []}
    response = await run_import(package, cookies={})
    assert response.status_code == 403
    assert "attribute_schema" in str(response.context["message"])
    assert import_service_spy == []


async def test_import_with_attribute_schema_passes_in_edit_mode(
    fake_settings: SimpleNamespace, import_service_spy: list[dict[str, object]]
) -> None:
    package = {"attribute_schema": [{"entity_type": "client", "name": "x"}]}
    response = await run_import(package, cookies={COOKIE_NAME: issue_edit_token()})
    assert response.status_code == 200
    assert len(import_service_spy) == 1


async def test_data_only_import_stays_open_when_locked(
    fake_settings: SimpleNamespace, import_service_spy: list[dict[str, object]]
) -> None:
    package = {"clients": [{"name": "test"}], "templates": []}
    response = await run_import(package, cookies={})
    assert response.status_code == 200
    assert len(import_service_spy) == 1


@pytest.fixture()
def project_repo_with(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a fake ProjectRepository knowing a fixed set of project names."""

    from app.routes import export_import as export_import_routes

    def install(known_names: set[str]) -> None:
        class FakeProjectRepo:
            def __init__(self, session: object) -> None:
                pass

            async def get_by_name(self, name: str) -> object | None:
                return SimpleNamespace(name=name) if name in known_names else None

        monkeypatch.setattr(export_import_routes, "ProjectRepository", FakeProjectRepo)

    return install


async def test_import_creating_projects_is_blocked_when_locked(
    fake_settings: SimpleNamespace,
    import_service_spy: list[dict[str, object]],
    project_repo_with: Any,
) -> None:
    # Creating a project is gated like attribute_schema — import must not be a
    # side door around the «Проекты» edit-mode lock.
    project_repo_with(set())
    package = {"templates": [{"id": "x", "name": "T", "project_name": "Новый"}]}
    response = await run_import(package, cookies={})
    assert response.status_code == 403
    assert "Новый" in str(response.context["message"])
    assert import_service_spy == []


async def test_import_creating_projects_passes_in_edit_mode(
    fake_settings: SimpleNamespace,
    import_service_spy: list[dict[str, object]],
    project_repo_with: Any,
) -> None:
    project_repo_with(set())
    package = {"templates": [{"id": "x", "name": "T", "project_name": "Новый"}]}
    response = await run_import(package, cookies={COOKIE_NAME: issue_edit_token()})
    assert response.status_code == 200
    assert len(import_service_spy) == 1


async def test_import_into_existing_projects_stays_open_when_locked(
    fake_settings: SimpleNamespace,
    import_service_spy: list[dict[str, object]],
    project_repo_with: Any,
) -> None:
    # Templates without project_name resolve to «Без проекта» — if it (and every
    # referenced name) already exists, nothing is created, so the import is open.
    from app.services.projects import DEFAULT_PROJECT_NAME

    project_repo_with({DEFAULT_PROJECT_NAME, "Альфа"})
    package = {
        "templates": [
            {"id": "x", "name": "T", "project_name": "Альфа"},
            {"id": "y", "name": "T2"},
        ]
    }
    response = await run_import(package, cookies={})
    assert response.status_code == 200
    assert len(import_service_spy) == 1


def test_every_mutating_settings_route_is_gated() -> None:
    gate_deps = [dep.dependency for dep in settings_routes.edit_router.dependencies]
    assert settings_routes.require_edit_mode in gate_deps

    gated = {
        (route.path, method)
        for route in settings_routes.edit_router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert gated == {
        ("/settings-htmx/attributes/new", "GET"),
        ("/settings-htmx/attributes/{attr_id}/edit", "GET"),
        ("/settings-htmx/attributes", "POST"),
        ("/settings-htmx/attributes/{attr_id}", "PUT"),
        ("/settings-htmx/attributes/{attr_id}", "DELETE"),
        ("/settings-htmx/attributes/reorder", "POST"),
        ("/settings-htmx/projects", "POST"),
        ("/settings-htmx/projects/{project_id}", "PUT"),
        ("/settings-htmx/projects/{project_id}", "DELETE"),
        ("/settings-htmx/import_policy", "PUT"),
        ("/settings-htmx/prompts/{key}", "PUT"),
    }

    open_paths = {
        route.path
        for route in settings_routes.router.routes
        if isinstance(route, APIRoute)
    }
    # Read-only views and the lock/unlock toggles stay public.
    assert open_paths == {
        "/settings",
        "/settings-htmx/attributes/table",
        "/settings-htmx/projects/table",
        "/settings-htmx/unlock",
        "/settings-htmx/lock",
    }


async def test_project_form_errors_surface_as_toasts(
    fake_settings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The project forms post with hx-swap='none', so failures must arrive as
    HX-Trigger toasts — a rendered HTML error body would be invisible."""

    # Schema failure (bad color) → 422 + error toast, no service call needed.
    response = await settings_routes.htmx_project_create(
        cast(Request, FakeFormRequest({"name": "P", "color": "red"})),
        session=cast(Any, object()),
    )
    assert response.status_code == 422
    payload = json.loads(response.headers["HX-Trigger"])
    assert payload["showToast"]["type"] == "error"

    # Domain failure (duplicate name) → its message in an error toast.
    from app.utils.errors import ValidationFailed

    class FakeProjectService:
        def __init__(self, session: object) -> None:
            pass

        async def create(self, data: Any) -> Any:
            raise ValidationFailed("Проект с таким именем уже существует")

    monkeypatch.setattr(settings_routes, "ProjectService", FakeProjectService)
    response = await settings_routes.htmx_project_create(
        cast(Request, FakeFormRequest({"name": "P", "color": "#112233"})),
        session=cast(Any, object()),
    )
    assert response.status_code == 422
    payload = json.loads(response.headers["HX-Trigger"])
    assert payload["showToast"]["type"] == "error"
    assert "уже существует" in payload["showToast"]["message"]
