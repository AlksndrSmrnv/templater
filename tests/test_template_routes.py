from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Match

from app.routes import templates_reg
from app.routes.templates_reg import router
from app.schemas.template import TemplateCreate
from app.utils.walker import Leaf


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


class FakeFormRequest:
    def __init__(self, form: dict[str, str]) -> None:
        self._form = form

    async def form(self) -> dict[str, str]:
        return self._form


def first_full_match_path(path: str, method: str = "GET") -> str:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }

    for route in router.routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            assert isinstance(route, APIRoute)
            return route.path

    raise AssertionError(f"No full route match for {method} {path}")


def test_template_htmx_table_route_is_matched_before_template_id_route() -> None:
    assert first_full_match_path("/templates-htmx/table") == "/templates-htmx/table"


@pytest.mark.asyncio
async def test_htmx_preview_validation_errors_retarget_form_errors() -> None:
    response = await templates_reg.htmx_preview(
        request=cast(Any, FakeFormRequest({"name": "", "format": "json", "content": "{}"})),
        templates=cast(Any, FakeTemplateRenderer()),
        session=cast(Any, object()),
    )

    assert response.status_code == 200
    assert response.headers["HX-Retarget"] == "#form-errors"
    assert response.headers["HX-Reswap"] == "innerHTML"


@pytest.mark.asyncio
async def test_htmx_create_validation_errors_retarget_review_errors() -> None:
    response = await templates_reg.htmx_create(
        request=cast(
            Any,
            FakeFormRequest(
                {
                    "name": "",
                    "description": "",
                    "format": "json",
                    "content": "{}",
                    "placeholders": "[]",
                    "llm_meta": "{}",
                }
            ),
        ),
        templates=cast(Any, FakeTemplateRenderer()),
        session=cast(Any, object()),
    )

    assert response.status_code == 200
    assert response.headers["HX-Retarget"] == "#review-errors"
    assert response.headers["HX-Reswap"] == "innerHTML"


@pytest.mark.asyncio
async def test_preview_template_includes_llm_debug_key(monkeypatch: pytest.MonkeyPatch) -> None:
    debug = {"system_prompt": "system", "user_prompt": "user", "response_text": "raw"}

    class FakeLlmContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    class FakeTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        @staticmethod
        def _extract_leaves(fmt: str, content: str) -> list[Leaf]:
            return [Leaf(location="/a", value="x")]

        async def analyze_content(
            self,
            *,
            fmt: str,
            original_content: str,
            llm_service: Any | None = None,
        ) -> dict[str, Any]:
            assert llm_service is not None
            return {
                "content": original_content,
                "placeholders": [],
                "llm_meta": {"summary": "ok"},
                "llm_debug": debug,
            }

        async def build_field_catalog(self) -> list[dict[str, str]]:
            return []

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)
    monkeypatch.setattr(templates_reg, "get_settings", lambda: SimpleNamespace(llm_active=True))
    monkeypatch.setattr(templates_reg, "llm_service", lambda: FakeLlmContext())
    monkeypatch.setattr(templates_reg, "render_template_html", lambda template: "<pre></pre>")

    response = await templates_reg.preview_template(
        TemplateCreate(name="T", format="json", content='{"a": "x"}'),
        session=cast(Any, object()),
    )

    assert response["llm_debug"] == debug
    assert response["llm_used"] is True


@pytest.mark.asyncio
async def test_preview_template_marks_llm_unused_when_no_debug_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeLlmContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    class FakeTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        @staticmethod
        def _extract_leaves(fmt: str, content: str) -> list[Leaf]:
            return []

        async def analyze_content(
            self,
            *,
            fmt: str,
            original_content: str,
            llm_service: Any | None = None,
        ) -> dict[str, Any]:
            assert llm_service is not None
            return {
                "content": original_content,
                "placeholders": [],
                "llm_meta": {"summary": "Пустой шаблон"},
                "llm_debug": None,
            }

        async def build_field_catalog(self) -> list[dict[str, str]]:
            return []

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)
    monkeypatch.setattr(templates_reg, "get_settings", lambda: SimpleNamespace(llm_active=True))
    monkeypatch.setattr(templates_reg, "llm_service", lambda: FakeLlmContext())
    monkeypatch.setattr(templates_reg, "render_template_html", lambda template: "<pre></pre>")

    response = await templates_reg.preview_template(
        TemplateCreate(name="T", format="json", content="{}"),
        session=cast(Any, object()),
    )

    assert response["llm_debug"] is None
    assert response["llm_used"] is False


@pytest.mark.asyncio
async def test_htmx_regenerate_returns_preview_without_committing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template_id = uuid.uuid4()
    analyzed = {
        "content": '{"a":"{{sender.fullName}}"}',
        "placeholders": [{"location": "/a", "mode": "mapped", "value": "{{sender.fullName}}"}],
        "llm_meta": {"summary": "LLM summary"},
        "llm_debug": {"system_prompt": "sys", "user_prompt": "usr", "response_text": "resp"},
    }

    class FakeTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        async def get(self, requested_id: uuid.UUID) -> Any:
            assert requested_id == template_id
            return SimpleNamespace(
                id=template_id,
                name="T",
                description="",
                format="json",
                content='{"a":"x"}',
                original_content='{"a":"x"}',
                placeholders=[],
                llm_meta={},
            )

        async def analyze_content(
            self,
            *,
            fmt: str,
            original_content: str,
            llm_service: Any | None = None,
        ) -> dict[str, Any]:
            assert fmt == "json"
            assert original_content == '{"a":"x"}'
            return analyzed

        async def build_field_catalog(self) -> list[dict[str, str]]:
            return []

    class FakeLlmContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)
    monkeypatch.setattr(templates_reg, "get_settings", lambda: SimpleNamespace(llm_active=True))
    monkeypatch.setattr(templates_reg, "llm_service", lambda: FakeLlmContext())
    monkeypatch.setattr(templates_reg, "render_template_html", lambda template: "<pre></pre>")

    response = await templates_reg.htmx_regenerate(
        template_id=template_id,
        request=cast(Any, FakeFormRequest({})),
        templates=cast(Any, FakeTemplateRenderer()),
        session=cast(Any, object()),
    )

    assert response.name == "partials/template_editor_response.html"
    assert response.context["llm_debug"] == analyzed["llm_debug"]
    assert response.context["template"].llm_meta == analyzed["llm_meta"]
    assert "Предпросмотр LLM обновлён" in response.headers["HX-Trigger"]


@pytest.mark.asyncio
async def test_htmx_update_persists_llm_meta_only_on_save(monkeypatch: pytest.MonkeyPatch) -> None:
    template_id = uuid.uuid4()
    calls: list[tuple[str, Any]] = []

    class FakeTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        async def update(self, requested_id: uuid.UUID, data: Any) -> Any:
            assert requested_id == template_id
            calls.append(("update", data))
            return SimpleNamespace()

        async def update_placeholders(
            self,
            requested_id: uuid.UUID,
            placeholders: list[dict[str, Any]],
        ) -> Any:
            assert requested_id == template_id
            calls.append(("update_placeholders", placeholders))
            return SimpleNamespace(
                id=template_id,
                name="T",
                description="",
                format="json",
                content='{"a":"{{sender.fullName}}"}',
                original_content='{"a":"x"}',
                placeholders=placeholders,
                llm_meta={"summary": "saved"},
            )

        async def build_field_catalog(self) -> list[dict[str, str]]:
            return []

    async def fake_commit_and_refresh(session: object, template: Any) -> Any:
        calls.append(("commit_and_refresh", template))
        return template

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)
    monkeypatch.setattr(templates_reg, "commit_and_refresh", fake_commit_and_refresh)
    monkeypatch.setattr(templates_reg, "render_template_html", lambda template: "<pre></pre>")

    response = await templates_reg.htmx_update(
        template_id=template_id,
        request=cast(
            Any,
            FakeFormRequest(
                {
                    "placeholders": '[{"location":"/a","mode":"mapped","value":"{{sender.fullName}}"}]',
                    "llm_meta": '{"summary":"preview"}',
                }
            ),
        ),
        templates=cast(Any, FakeTemplateRenderer()),
        session=cast(Any, object()),
    )

    assert response.name == "partials/template_editor_response.html"
    assert calls[0][0] == "update"
    assert calls[0][1].llm_meta == {"summary": "preview"}
    assert calls[1][0] == "update_placeholders"
    assert calls[2][0] == "commit_and_refresh"


@pytest.mark.asyncio
async def test_htmx_update_rejects_non_object_llm_meta() -> None:
    response = await templates_reg.htmx_update(
        template_id=uuid.uuid4(),
        request=cast(
            Any,
            FakeFormRequest(
                {
                    "placeholders": "[]",
                    "llm_meta": "[]",
                }
            ),
        ),
        templates=cast(Any, FakeTemplateRenderer()),
        session=cast(Any, object()),
    )

    assert response.name == "partials/form_errors.html"
    assert response.status_code == 422
    assert response.context["message"] == "Поле llm_meta должно быть JSON-объектом"
