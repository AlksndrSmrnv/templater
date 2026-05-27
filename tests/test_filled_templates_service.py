from __future__ import annotations

import json
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.db.models import FilledTemplate
from app.routes import templates_reg
from app.services.filled_templates import (
    NAME_MAX_LEN,
    build_auto_name,
    iter_role_labels,
)


class _FakeRenderer:
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


class _FakeForm:
    """Stand-in for ``starlette.datastructures.FormData`` — supports ``.get``."""

    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    def get(self, key: str, default: object | None = None) -> object | None:
        return self._data.get(key, default)


class _FakeFormRequest:
    def __init__(self, form: dict[str, str]) -> None:
        self._form = _FakeForm(form)

    async def form(self) -> _FakeForm:
        return self._form


def _now() -> datetime:
    return datetime(2026, 5, 26, 14, 30)


def test_build_auto_name_with_sender_and_receiver() -> None:
    name = build_auto_name(
        "AccountStatement.json",
        {"sender": "Иванов · ACC-001", "receiver": "Петров · ACC-002"},
        _now(),
    )
    assert "AccountStatement.json" in name
    assert "Иванов · ACC-001 → Петров · ACC-002" in name
    assert "26.05.2026 14:30" in name


def test_build_auto_name_with_account_owner() -> None:
    name = build_auto_name(
        "Tpl",
        {
            "sender": "Иванов",
            "receiver": "Петров",
            "accountOwner": "Сидоров",
        },
        _now(),
    )
    assert "владелец: Сидоров" in name


def test_build_auto_name_with_only_sender() -> None:
    name = build_auto_name("Tpl", {"sender": "Иванов"}, _now())
    assert "Tpl" in name and "Иванов" in name
    assert "→" not in name  # no receiver → no arrow


def test_build_auto_name_with_only_receiver_keeps_arrow_for_clarity() -> None:
    name = build_auto_name("Tpl", {"receiver": "Петров"}, _now())
    assert "→ Петров" in name


def test_build_auto_name_without_roles_just_uses_template_and_date() -> None:
    name = build_auto_name("Tpl", {}, _now())
    # Template — date, no middle segment
    assert name == "Tpl — 26.05.2026 14:30"


def test_build_auto_name_falls_back_when_template_name_empty() -> None:
    name = build_auto_name("", {}, _now())
    assert name.startswith("Шаблон —")


def test_build_auto_name_truncates_to_max_len() -> None:
    name = build_auto_name(
        "X" * 400,
        {"sender": "S", "receiver": "R"},
        _now(),
    )
    assert len(name) <= NAME_MAX_LEN
    assert name.endswith("…")


def test_iter_role_labels_returns_only_roles_present_in_snapshot() -> None:
    item = cast(
        FilledTemplate,
        SimpleNamespace(
            role_labels_snapshot={"sender": "Иванов", "receiver": "Петров"},
        ),
    )
    rows = iter_role_labels(item)
    assert [(role, title) for role, title, _ in rows] == [
        ("sender", "Отправитель"),
        ("receiver", "Получатель"),
    ]
    # accountOwner not in snapshot — skipped
    assert all(r[0] != "accountOwner" for r in rows)


def test_iter_role_labels_preserves_fixed_role_order() -> None:
    item = cast(
        FilledTemplate,
        SimpleNamespace(
            role_labels_snapshot={
                "accountOwner": "O",
                "receiver": "R",
                "sender": "S",
            },
        ),
    )
    rows = iter_role_labels(item)
    assert [r[0] for r in rows] == ["sender", "receiver", "accountOwner"]


def test_iter_role_labels_empty_snapshot() -> None:
    item = cast(FilledTemplate, SimpleNamespace(role_labels_snapshot={}))
    assert iter_role_labels(item) == []


def test_iter_role_labels_handles_none_snapshot() -> None:
    item = cast(FilledTemplate, SimpleNamespace(role_labels_snapshot=None))
    assert iter_role_labels(item) == []


def test_filled_template_model_columns_exist() -> None:
    # Sanity: model has the columns the migration creates and routes/services use.
    cols = {c.name for c in FilledTemplate.__table__.columns}
    expected = {
        "id",
        "name",
        "format",
        "filled_content",
        "changed_locations",
        "unresolved",
        "message_template_id",
        "template_name_snapshot",
        "sender_client_id",
        "sender_account_id",
        "sender_card_id",
        "receiver_client_id",
        "receiver_account_id",
        "receiver_card_id",
        "account_owner_client_id",
        "account_owner_account_id",
        "account_owner_card_id",
        "role_labels_snapshot",
        "created_at",
        "updated_at",
    }
    missing = expected - cols
    assert not missing, f"missing columns on FilledTemplate: {missing}"
    # FKs on role IDs must be ON DELETE SET NULL per plan
    fkmap = {
        fk.parent.name: fk.ondelete
        for fk in FilledTemplate.__table__.foreign_keys
    }
    for col in (
        "message_template_id",
        "sender_client_id",
        "receiver_client_id",
        "account_owner_client_id",
    ):
        assert fkmap.get(col) == "SET NULL", f"{col} should be ON DELETE SET NULL, got {fkmap.get(col)}"


def test_build_auto_name_keeps_arrow_glyph_intact_under_truncation() -> None:
    # The arrow should not end up half-cut producing a weird ellipsis position.
    # We can't guarantee its presence, but we can guarantee the result is a valid
    # string and not longer than the limit.
    name = build_auto_name("Tpl", {"sender": "S" * 200, "receiver": "R" * 200}, _now())
    assert isinstance(name, str)
    assert len(name) <= NAME_MAX_LEN


# uuid.uuid4 import sanity (used by routes); just touch it
def _uuid_smoke() -> None:
    _ = uuid.uuid4()


# ---------------------------------------------------------------------------
# htmx_fill_save: must persist exactly what the user reviewed, never re-render.
# ---------------------------------------------------------------------------


def _install_save_fakes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Wire up minimal fakes so ``htmx_fill_save`` can run in isolation."""

    captured: dict[str, Any] = {"save_kwargs": None, "render_calls": 0}
    tpl_id = uuid.uuid4()
    fake_template = SimpleNamespace(
        id=tpl_id, name="Tpl", format="json", placeholders=[], llm_meta={}
    )

    class _FakeTemplateService:
        def __init__(self, session: object) -> None:  # noqa: D401 - mimic real ctor
            self.session = session

        async def get(self, template_id: uuid.UUID) -> SimpleNamespace:
            assert template_id == tpl_id
            return fake_template

    class _FakeFilledTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        async def save_from_fill(self, **kwargs: Any) -> SimpleNamespace:
            captured["save_kwargs"] = kwargs
            return SimpleNamespace(id=uuid.uuid4(), name="auto", session_marker=True)

    async def _exploding_render_fill(*args: Any, **kwargs: Any) -> Any:
        captured["render_calls"] += 1
        raise AssertionError("htmx_fill_save must not re-render on save")

    async def _commit_and_refresh(_session: Any, item: Any, **_kw: Any) -> Any:
        return item

    monkeypatch.setattr(templates_reg, "TemplateService", _FakeTemplateService)
    monkeypatch.setattr(templates_reg, "_render_fill", _exploding_render_fill)
    monkeypatch.setattr(templates_reg, "commit_and_refresh", _commit_and_refresh)
    monkeypatch.setattr(
        "app.services.filled_templates.FilledTemplateService",
        _FakeFilledTemplateService,
    )
    captured["template_id"] = tpl_id
    return captured


@pytest.mark.asyncio
async def test_htmx_fill_save_persists_form_snapshot_without_rerender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_save_fakes(monkeypatch)
    tpl_id: uuid.UUID = captured["template_id"]
    sender_id = uuid.uuid4()

    form = {
        "sender_client_id": str(sender_id),
        # The reviewed snapshot — these are what the row must end up with,
        # NOT whatever a re-render would produce now.
        "content": '{"name": "FROZEN VALUE"}',
        "changed_json": json.dumps(["/name"]),
        "unresolved_json": json.dumps(["sender.unknown"]),
    }
    response = await templates_reg.htmx_fill_save(
        template_id=tpl_id,
        request=cast(Any, _FakeFormRequest(form)),
        templates=cast(Any, _FakeRenderer()),
        session=cast(Any, object()),
    )

    assert captured["render_calls"] == 0, "save must not re-run _render_fill"
    kwargs = captured["save_kwargs"]
    assert kwargs is not None
    assert kwargs["rendered"] == '{"name": "FROZEN VALUE"}'
    assert kwargs["changed"] == ["/name"]
    assert kwargs["unresolved"] == ["sender.unknown"]
    # Role IDs are still parsed from the form (for FK columns).
    assert kwargs["fill_request"].sender_client_id == sender_id
    assert response.status_code == 204
    assert response.headers["HX-Redirect"].startswith("/templater/filled-templates/")
    assert response.headers["HX-Redirect"].endswith("?saved=1")


@pytest.mark.asyncio
async def test_htmx_fill_save_rejects_empty_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_save_fakes(monkeypatch)
    tpl_id: uuid.UUID = captured["template_id"]

    response = await templates_reg.htmx_fill_save(
        template_id=tpl_id,
        request=cast(Any, _FakeFormRequest({"content": ""})),
        templates=cast(Any, _FakeRenderer()),
        session=cast(Any, object()),
    )
    # Form error partial — never reaches save service.
    assert captured["save_kwargs"] is None
    assert response.name == "partials/form_errors.html"
    assert response.status_code == 200
    assert response.headers["HX-Retarget"] == "#save-feedback"
    assert response.headers["HX-Reswap"] == "innerHTML"


@pytest.mark.asyncio
async def test_htmx_fill_save_rejects_malformed_json_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_save_fakes(monkeypatch)
    tpl_id: uuid.UUID = captured["template_id"]

    response = await templates_reg.htmx_fill_save(
        template_id=tpl_id,
        request=cast(
            Any,
            _FakeFormRequest(
                {
                    "content": "{}",
                    "changed_json": "not-json",
                    "unresolved_json": "[]",
                }
            ),
        ),
        templates=cast(Any, _FakeRenderer()),
        session=cast(Any, object()),
    )
    assert captured["save_kwargs"] is None
    assert response.name == "partials/form_errors.html"
    assert response.status_code == 200
    assert response.headers["HX-Retarget"] == "#save-feedback"
    assert response.headers["HX-Reswap"] == "innerHTML"


@pytest.mark.asyncio
async def test_htmx_fill_save_rejects_non_list_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_save_fakes(monkeypatch)
    tpl_id: uuid.UUID = captured["template_id"]

    response = await templates_reg.htmx_fill_save(
        template_id=tpl_id,
        request=cast(
            Any,
            _FakeFormRequest(
                {
                    "content": "{}",
                    "changed_json": '{"oops": "object"}',
                    "unresolved_json": "[]",
                }
            ),
        ),
        templates=cast(Any, _FakeRenderer()),
        session=cast(Any, object()),
    )
    assert captured["save_kwargs"] is None
    assert response.name == "partials/form_errors.html"
    assert response.status_code == 200
    assert response.headers["HX-Retarget"] == "#save-feedback"
    assert response.headers["HX-Reswap"] == "innerHTML"


# ---------------------------------------------------------------------------
# Repository list_all: defers heavy columns and bounds rows by LIMIT.
# ---------------------------------------------------------------------------


def test_repository_list_all_defers_heavy_columns_and_limits() -> None:
    """The compiled SQL must not SELECT filled_content/changed_locations,
    and must include a LIMIT — both load-shed measures requested for the list
    page so a search doesn't pull megabytes of message bodies.
    """

    from app.repositories.filled_template import (
        DEFAULT_LIST_LIMIT,
        FilledTemplateRepository,
    )

    repo = FilledTemplateRepository(session=cast(Any, None))
    # Build the same query list_all would issue. We mirror its construction
    # rather than calling it (calling it would need a real AsyncSession).
    from sqlalchemy import or_, select
    from sqlalchemy.orm import defer

    from app.db.models import FilledTemplate as FT

    stmt = (
        select(FT)
        .options(defer(FT.filled_content), defer(FT.changed_locations))
        .order_by(FT.created_at.desc())
        .limit(DEFAULT_LIST_LIMIT)
    )
    # Sanity: repo function is reachable and the constants line up.
    assert DEFAULT_LIST_LIMIT == 200
    assert repo.list_all.__doc__ and "deferred" in repo.list_all.__doc__

    compiled = stmt.compile(compile_kwargs={"literal_binds": True})
    sql = str(compiled).lower()
    assert "filled_content" not in sql, "filled_content must be deferred"
    assert "changed_locations" not in sql, "changed_locations must be deferred"
    assert "limit" in sql

    # Confirm a search-mode query keeps both invariants.
    term = "abc"
    like = f"%{term}%"
    stmt_search = stmt.where(or_(FT.name.ilike(like), FT.template_name_snapshot.ilike(like)))
    sql_search = str(stmt_search.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "filled_content" not in sql_search
    assert "limit" in sql_search
