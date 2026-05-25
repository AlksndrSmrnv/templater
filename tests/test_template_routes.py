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


def test_template_catalog_route_is_matched_before_template_id_route() -> None:
    assert first_full_match_path("/api/templates/catalog") == "/api/templates/catalog"


@pytest.mark.asyncio
async def test_api_preview_includes_llm_debug_key(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)
    monkeypatch.setattr(templates_reg, "get_settings", lambda: SimpleNamespace(llm_active=True))
    monkeypatch.setattr(templates_reg, "llm_service", lambda: FakeLlmContext())
    monkeypatch.setattr(templates_reg, "render_template_html", lambda template: "<pre></pre>")

    response = await templates_reg.api_preview(
        TemplateCreate(name="T", format="json", content='{"a": "x"}'),
        session=cast(Any, object()),
    )

    assert response["llm_debug"] == debug
    assert response["llm_used"] is True


@pytest.mark.asyncio
async def test_api_preview_marks_llm_unused_when_no_debug_returned(
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

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)
    monkeypatch.setattr(templates_reg, "get_settings", lambda: SimpleNamespace(llm_active=True))
    monkeypatch.setattr(templates_reg, "llm_service", lambda: FakeLlmContext())
    monkeypatch.setattr(templates_reg, "render_template_html", lambda template: "<pre></pre>")

    response = await templates_reg.api_preview(
        TemplateCreate(name="T", format="json", content="{}"),
        session=cast(Any, object()),
    )

    assert response["llm_debug"] is None
    assert response["llm_used"] is False
