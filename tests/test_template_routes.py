from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Match

from app.routes import templates_reg
from app.routes.templates_reg import router
from app.schemas.template import TemplateCreate
from app.utils.errors import ValidationFailed
from app.utils.signing import sign_processed
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


class FakeProjectRepository:
    """ProjectRepository double: every id resolves to a stub project."""

    def __init__(self, session: object) -> None:
        self.session = session

    async def get(self, project_id: uuid.UUID) -> SimpleNamespace:
        return SimpleNamespace(id=project_id, name="P", color="#1E88E5")

    async def list_all(self) -> list[SimpleNamespace]:
        return []


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


def test_sign_processed_proof_binds_content_and_meta_and_is_not_forgeable() -> None:
    from app.utils.signing import verify_processed

    content = '{"a": "x"}'
    meta = {"summary": "ok"}
    proof = sign_processed(content, meta)

    assert verify_processed(content, meta, proof) is True
    # A proof must not validate a different body, different analysis, or junk.
    assert verify_processed('{"a": "y"}', meta, proof) is False
    assert verify_processed(content, {"summary": "forged"}, proof) is False
    assert verify_processed(content, {}, proof) is False
    assert verify_processed(content, meta, "deadbeef") is False
    assert verify_processed(content, meta, "") is False
    assert verify_processed(content, meta, None) is False


def test_sign_processed_is_newline_canonical() -> None:
    """Preview receives CRLF content (multipart serialisation), create receives
    LF (urlencoded htmx submit) — the proof must survive that round trip."""

    from app.utils.signing import verify_processed

    crlf = '{\r\n  "a": "x"\r\n}'
    lf = crlf.replace("\r\n", "\n")
    meta = {"summary": "ok"}

    proof = sign_processed(crlf, meta)
    assert verify_processed(lf, meta, proof) is True
    assert verify_processed(crlf, meta, proof) is True
    # Canonicalisation covers newline flavour only — not other edits.
    assert verify_processed(lf.replace("x", "y"), meta, proof) is False


def test_template_htmx_tree_route_is_matched_before_template_id_route() -> None:
    assert first_full_match_path("/templates-htmx/tree") == "/templates-htmx/tree"


def test_template_htmx_panel_route_matches() -> None:
    template_id = uuid.uuid4()
    assert (
        first_full_match_path(f"/templates-htmx/{template_id}/panel")
        == "/templates-htmx/{template_id}/panel"
    )


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
@pytest.mark.parametrize(
    ("content", "proof_content", "proof_meta", "expected_status"),
    [
        # Real upload flow: parsable body + proof matching submitted content+meta.
        ('{"a": "x"}', '{"a": "x"}', {"summary": "ok"}, "processed"),
        # No proof at all → not processed (can't sneak past _require_processed).
        ('{"a": "x"}', None, None, None),
        # Proof minted for different content doesn't validate.
        ('{"a": "x"}', '{"a": "y"}', {"summary": "ok"}, None),
        # Proof minted for different analysis: client swapped llm_meta after
        # obtaining a valid proof → must NOT stay processed (the P2.2 attack).
        ('{"a": "x"}', '{"a": "x"}', {"summary": "different"}, None),
        # Even a valid proof can't mark a non-parsable body processed.
        ("not json at all", "not json at all", {"summary": "ok"}, None),
    ],
)
async def test_htmx_create_marks_processed_only_with_valid_proof(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    proof_content: str | None,
    proof_meta: dict[str, Any] | None,
    expected_status: str | None,
) -> None:
    """Only a parsable body whose submitted content AND llm_meta match the
    server's preview proof is marked processed — a client-crafted POST cannot
    forge the flag, nor swap in fake analysis under a stolen proof. The form
    always submits llm_meta={"summary": "ok"}; the proof is minted separately
    so each case can mismatch one input."""

    submitted_meta = {"summary": "ok"}
    saved: dict[str, Any] = {}

    class FakeTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        async def create(self, data: Any) -> Any:
            return SimpleNamespace(
                id=uuid.uuid4(),
                format="json",
                content=data.content,
                original_content=data.content,
                placeholders=[],
                llm_meta={},
            )

        @staticmethod
        def regenerate_content(template: Any) -> str:
            return template.content

    async def fake_commit(session: object, item: Any) -> Any:
        saved["llm_meta"] = item.llm_meta
        return item

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)
    monkeypatch.setattr(templates_reg, "commit_and_refresh", fake_commit)
    monkeypatch.setattr(templates_reg, "normalize_placeholders", lambda p: p)
    monkeypatch.setattr(templates_reg, "placeholders_have_account_owner", lambda p: False)

    llm_proof = "" if proof_content is None else sign_processed(proof_content, proof_meta)

    response = await templates_reg.htmx_create(
        request=cast(
            Any,
            FakeFormRequest(
                {
                    "name": "T",
                    "description": "",
                    "format": "json",
                    "content": content,
                    "placeholders": "[]",
                    "llm_meta": json.dumps(submitted_meta),
                    "llm_proof": llm_proof,
                    "project_id": str(uuid.uuid4()),
                }
            ),
        ),
        templates=cast(Any, FakeTemplateRenderer()),
        session=cast(Any, object()),
    )

    assert response.status_code == 204
    assert saved["llm_meta"].get("import_status") == expected_status
    # Client-supplied analysis is preserved alongside the (verified) flag.
    assert saved["llm_meta"]["summary"] == "ok"


@pytest.mark.asyncio
async def test_htmx_create_strips_client_supplied_import_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A POST can't smuggle import_status=processed through llm_meta: the
    reserved key is stripped, and without a valid proof it stays unset."""

    saved: dict[str, Any] = {}

    class FakeTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        async def create(self, data: Any) -> Any:
            return SimpleNamespace(
                id=uuid.uuid4(),
                format="json",
                content=data.content,
                original_content=data.content,
                placeholders=[],
                llm_meta={},
            )

        @staticmethod
        def regenerate_content(template: Any) -> str:
            return template.content

    async def fake_commit(session: object, item: Any) -> Any:
        saved["llm_meta"] = item.llm_meta
        return item

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)
    monkeypatch.setattr(templates_reg, "commit_and_refresh", fake_commit)
    monkeypatch.setattr(templates_reg, "normalize_placeholders", lambda p: p)
    monkeypatch.setattr(templates_reg, "placeholders_have_account_owner", lambda p: False)

    response = await templates_reg.htmx_create(
        request=cast(
            Any,
            FakeFormRequest(
                {
                    "name": "T",
                    "description": "",
                    "format": "json",
                    "content": '{"a": "x"}',
                    "placeholders": "[]",
                    # Forged: status smuggled in, no proof supplied.
                    "llm_meta": '{"summary": "ok", "import_status": "processed"}',
                    "llm_proof": "",
                    "project_id": str(uuid.uuid4()),
                }
            ),
        ),
        templates=cast(Any, FakeTemplateRenderer()),
        session=cast(Any, object()),
    )

    assert response.status_code == 204
    assert "import_status" not in saved["llm_meta"]
    assert saved["llm_meta"]["summary"] == "ok"


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
    monkeypatch.setattr(templates_reg, "llm_service", lambda *a, **k: FakeLlmContext())
    monkeypatch.setattr(templates_reg, "render_template_html", lambda template: "<pre></pre>")
    monkeypatch.setattr(templates_reg, "ProjectRepository", FakeProjectRepository)

    response = await templates_reg.preview_template(
        TemplateCreate(name="T", format="json", content='{"a": "x"}', project_id=uuid.uuid4()),
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
    monkeypatch.setattr(templates_reg, "llm_service", lambda *a, **k: FakeLlmContext())
    monkeypatch.setattr(templates_reg, "render_template_html", lambda template: "<pre></pre>")
    monkeypatch.setattr(templates_reg, "ProjectRepository", FakeProjectRepository)

    response = await templates_reg.preview_template(
        TemplateCreate(name="T", format="json", content="{}", project_id=uuid.uuid4()),
        session=cast(Any, object()),
    )

    assert response["llm_debug"] is None
    assert response["llm_used"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "method_name", "toast"),
    [
        ("htmx_regenerate_meta", "regenerate_meta_and_persist", "Метаинформация обновлена"),
        ("htmx_regenerate_fields", "regenerate_fields_and_persist", "Шаблон обработан заново"),
    ],
)
async def test_granular_reprocess_persists_and_renders_panel(
    monkeypatch: pytest.MonkeyPatch,
    handler_name: str,
    method_name: str,
    toast: str,
) -> None:
    template_id = uuid.uuid4()
    template = SimpleNamespace(id=template_id, name="T")
    calls: list[str] = []

    class FakeTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        async def get(self, requested_id: uuid.UUID) -> Any:
            assert requested_id == template_id
            return template

    async def record(self: Any, tpl: Any, *, llm_service: Any | None = None) -> Any:
        calls.append(method_name)
        return tpl

    # Each granular route must dispatch to its own service method.
    setattr(FakeTemplateService, method_name, record)

    class FakeLlmContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    async def fake_commit(session: object, item: Any) -> Any:
        return item

    async def fake_panel_context(session: object, tpl: Any) -> dict[str, Any]:
        return {"template": tpl}

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)
    monkeypatch.setattr(templates_reg, "llm_service", lambda *a, **k: FakeLlmContext())
    monkeypatch.setattr(templates_reg, "commit_and_refresh", fake_commit)
    monkeypatch.setattr(templates_reg, "_template_panel_context", fake_panel_context)

    handler = getattr(templates_reg, handler_name)
    response = await handler(
        template_id=template_id,
        request=cast(Any, FakeFormRequest({})),
        templates=cast(Any, FakeTemplateRenderer()),
        session=cast(Any, object()),
    )

    assert calls == [method_name]
    assert response.name == "partials/template_panel.html"
    trigger = json.loads(response.headers["HX-Trigger"])
    assert toast in trigger["showToast"]["message"]


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


@pytest.mark.asyncio
async def test_htmx_fill_render_reports_unparsable_body(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(session: object, template_id: uuid.UUID, data: object) -> Any:
        raise ValidationFailed("Шаблон не парсится как JSON")

    monkeypatch.setattr(templates_reg, "_render_fill", boom)

    response = await templates_reg.htmx_fill_render(
        template_id=uuid.uuid4(),
        request=cast(Any, FakeFormRequest({})),
        templates=cast(Any, FakeTemplateRenderer()),
        session=cast(Any, object()),
    )

    assert response.name == "partials/form_errors.html"
    assert response.status_code == 200
    assert "не парсится" in response.context["message"]


@pytest.mark.asyncio
async def test_htmx_process_llm_reports_plain_llm_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    template_id = uuid.uuid4()

    class FakeTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        async def get(self, requested_id: uuid.UUID) -> Any:
            return SimpleNamespace(id=requested_id, name="T")

        async def analyze_and_persist(self, template: Any, *, llm_service: Any | None = None) -> Any:
            raise RuntimeError("client failed after retries")

    class FakeLlmContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    class FakeSession:
        async def rollback(self) -> None:
            return None

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)
    monkeypatch.setattr(templates_reg, "llm_service", lambda *a, **k: FakeLlmContext())

    response = await templates_reg.htmx_process_llm(
        template_id=template_id,
        request=cast(Any, FakeFormRequest({})),
        templates=cast(Any, FakeTemplateRenderer()),
        session=cast(Any, FakeSession()),
    )

    assert response.name == "partials/form_errors.html"
    assert response.status_code == 200
    assert "LLM не смогла" in response.context["message"]
    assert response.headers["HX-Retarget"] == "#panel-errors"


@pytest.mark.asyncio
async def test_htmx_process_llm_persistence_failure_is_not_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB failure after a successful analysis must propagate, not be reported as an LLM failure."""
    template_id = uuid.uuid4()

    class FakeTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        async def get(self, requested_id: uuid.UUID) -> Any:
            return SimpleNamespace(id=requested_id, name="T")

        async def analyze_and_persist(self, template: Any, *, llm_service: Any | None = None) -> Any:
            return None  # analysis succeeds

    class FakeLlmContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    async def boom(session: object, item: object, **kwargs: object) -> Any:
        raise RuntimeError("database is down")

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)
    monkeypatch.setattr(templates_reg, "llm_service", lambda *a, **k: FakeLlmContext())
    monkeypatch.setattr(templates_reg, "commit_and_refresh", boom)

    with pytest.raises(RuntimeError, match="database is down"):
        await templates_reg.htmx_process_llm(
            template_id=template_id,
            request=cast(Any, FakeFormRequest({})),
            templates=cast(Any, FakeTemplateRenderer()),
            session=cast(Any, object()),
        )


@pytest.mark.asyncio
async def test_htmx_regenerate_meta_reports_plain_llm_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    template_id = uuid.uuid4()

    class FakeTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        async def get(self, requested_id: uuid.UUID) -> Any:
            return SimpleNamespace(id=requested_id, name="T")

        async def regenerate_meta_and_persist(self, template: Any, *, llm_service: Any | None = None) -> Any:
            raise RuntimeError("client failed after retries")

    class FakeLlmContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    class FakeSession:
        async def rollback(self) -> None:
            return None

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)
    monkeypatch.setattr(templates_reg, "llm_service", lambda *a, **k: FakeLlmContext())

    response = await templates_reg.htmx_regenerate_meta(
        template_id=template_id,
        request=cast(Any, FakeFormRequest({})),
        templates=cast(Any, FakeTemplateRenderer()),
        session=cast(Any, FakeSession()),
    )

    assert response.name == "partials/form_errors.html"
    assert response.status_code == 200
    assert "LLM не смогла" in response.context["message"]
    # Granular reprocess errors land in the panel's dedicated container.
    assert response.headers["HX-Retarget"] == "#panel-errors"
    assert response.headers["HX-Reswap"] == "innerHTML"


@pytest.mark.asyncio
async def test_htmx_set_project_refreshes_tree_and_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reassigning the project re-renders the panel AND triggers a sidebar
    refresh — otherwise the tree keeps showing the old badge."""

    template_id = uuid.uuid4()
    project_id = uuid.uuid4()
    captured: dict[str, Any] = {}

    class FakeTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        async def update(self, tid: uuid.UUID, data: Any) -> Any:
            captured["project_id"] = data.project_id
            return SimpleNamespace(id=tid)

    async def fake_commit(session: object, item: Any) -> Any:
        return item

    async def fake_panel_context(session: object, template: Any) -> dict[str, Any]:
        return {"template": template}

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)
    monkeypatch.setattr(templates_reg, "commit_and_refresh", fake_commit)
    monkeypatch.setattr(templates_reg, "_template_panel_context", fake_panel_context)

    response = await templates_reg.htmx_set_project(
        template_id,
        request=cast(Any, FakeFormRequest({"project_id": str(project_id)})),
        templates=cast(Any, FakeTemplateRenderer()),
        session=cast(Any, object()),
    )

    assert captured["project_id"] == project_id
    assert response.name == "partials/template_panel.html"
    trigger = json.loads(response.headers["HX-Trigger"])
    assert "refresh-tree" in trigger


@pytest.mark.asyncio
async def test_htmx_set_project_rejects_missing_project_id() -> None:
    response = await templates_reg.htmx_set_project(
        uuid.uuid4(),
        request=cast(Any, FakeFormRequest({"project_id": ""})),
        templates=cast(Any, FakeTemplateRenderer()),
        session=cast(Any, object()),
    )
    # Error rendered into the panel errors slot, not a 500.
    assert response.status_code == 200
    assert response.headers["HX-Retarget"] == "#panel-errors"


# ---------- PUT /templates-htmx/{id}/content (manual body edit) ----------


def test_template_htmx_content_route_does_not_shadow_update_route() -> None:
    template_id = uuid.uuid4()
    assert (
        first_full_match_path(f"/templates-htmx/{template_id}/content", method="PUT")
        == "/templates-htmx/{template_id}/content"
    )
    assert (
        first_full_match_path(f"/templates-htmx/{template_id}", method="PUT")
        == "/templates-htmx/{template_id}"
    )


@pytest.mark.asyncio
async def test_htmx_edit_content_success_renders_panel_with_toast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template_id = uuid.uuid4()
    template = SimpleNamespace(id=template_id, name="T")
    calls: list[tuple[str, Any]] = []

    class FakeTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        async def edit_content(self, requested_id: uuid.UUID, content: str) -> Any:
            assert requested_id == template_id
            calls.append(("edit_content", content))
            return template

    async def fake_commit(session: object, item: Any) -> Any:
        calls.append(("commit_and_refresh", item))
        return item

    async def fake_panel_context(session: object, tpl: Any) -> dict[str, Any]:
        return {"template": tpl}

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)
    monkeypatch.setattr(templates_reg, "commit_and_refresh", fake_commit)
    monkeypatch.setattr(templates_reg, "_template_panel_context", fake_panel_context)

    response = await templates_reg.htmx_edit_content(
        template_id=template_id,
        request=cast(Any, FakeFormRequest({"content": '{"a": "x"}'})),
        templates=cast(Any, FakeTemplateRenderer()),
        session=cast(Any, object()),
    )

    assert calls == [("edit_content", '{"a": "x"}'), ("commit_and_refresh", template)]
    assert response.name == "partials/template_panel.html"
    trigger = json.loads(response.headers["HX-Trigger"])
    assert "Тело шаблона обновлено" in trigger["showToast"]["message"]


@pytest.mark.asyncio
async def test_htmx_edit_content_parse_error_retargets_and_carries_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.utils.errors import ContentParseFailed

    template_id = uuid.uuid4()

    class FakeTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        async def edit_content(self, requested_id: uuid.UUID, content: str) -> Any:
            raise ContentParseFailed("Невалидный JSON: Expecting value", line=3, col=5)

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)

    response = await templates_reg.htmx_edit_content(
        template_id=template_id,
        request=cast(Any, FakeFormRequest({"content": "{broken"})),
        templates=cast(Any, FakeTemplateRenderer()),
        session=cast(Any, object()),
    )

    # htmx 2 does not swap non-2xx responses → error partial ships as 200
    # retargeted into the still-mounted editor.
    assert response.status_code == 200
    assert response.name == "partials/form_errors.html"
    assert response.headers["HX-Retarget"] == "#body-edit-errors"
    assert response.headers["HX-Reswap"] == "innerHTML"
    event = json.loads(response.headers["HX-Trigger"])["template-body-error"]
    assert event["line"] == 3
    assert event["col"] == 5
    assert "Невалидный JSON" in event["message"]


@pytest.mark.asyncio
async def test_htmx_edit_content_plain_validation_error_has_no_line_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template_id = uuid.uuid4()

    class FakeTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        async def edit_content(self, requested_id: uuid.UUID, content: str) -> Any:
            raise ValidationFailed("Пустой шаблон")

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)

    response = await templates_reg.htmx_edit_content(
        template_id=template_id,
        request=cast(Any, FakeFormRequest({"content": "  "})),
        templates=cast(Any, FakeTemplateRenderer()),
        session=cast(Any, object()),
    )

    assert response.status_code == 200
    assert response.headers["HX-Retarget"] == "#body-edit-errors"
    assert "HX-Trigger" not in response.headers
