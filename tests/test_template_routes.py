from __future__ import annotations

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
