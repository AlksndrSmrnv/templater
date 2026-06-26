from __future__ import annotations

import json
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.db.models import FilledTemplate, RequestChainStep
from app.routes import templates_reg
from app.services.filled_templates import (
    NAME_MAX_LEN,
    _surname,
    build_auto_name,
    build_short_name,
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


def test_surname_takes_first_word() -> None:
    assert _surname("Иванов Иван Иванович") == "Иванов"
    assert _surname("ООО Ромашка") == "ООО"
    assert _surname("  Петров  ") == "Петров"
    assert _surname("") == ""
    assert _surname("   ") == ""


def test_build_short_name_sender_and_receiver() -> None:
    name = build_short_name(
        "Перевод",
        {"sender": ("Иванов", "ACC-001"), "receiver": ("Петров", "ACC-002")},
    )
    assert name == "Перевод Иванов ACC-001 Петров ACC-002"
    assert "—" not in name  # no date / separators of the old format
    assert "→" not in name


def test_build_short_name_only_sender_without_number() -> None:
    name = build_short_name("Tpl", {"sender": ("Иванов", "")})
    assert name == "Tpl Иванов"


def test_build_short_name_appends_owner_last() -> None:
    name = build_short_name(
        "Tpl",
        {
            "sender": ("Иванов", "ACC-1"),
            "receiver": ("Петров", "ACC-2"),
            "accountOwner": ("Сидоров", "ACC-3"),
        },
    )
    assert name == "Tpl Иванов ACC-1 Петров ACC-2 Сидоров ACC-3"


def test_build_short_name_without_roles_is_just_template() -> None:
    assert build_short_name("Tpl", {}) == "Tpl"


def test_build_short_name_falls_back_when_template_empty() -> None:
    assert build_short_name("", {}) == "Шаблон"


def test_build_short_name_truncates_to_max_len() -> None:
    name = build_short_name("X" * 400, {"sender": ("Y" * 100, "Z" * 100)})
    assert len(name) <= NAME_MAX_LEN
    assert name.endswith("…")


def test_chain_step_model_has_role_columns_set_null() -> None:
    cols = {c.name for c in RequestChainStep.__table__.columns}
    expected = {
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
    }
    assert not (expected - cols), f"missing on RequestChainStep: {expected - cols}"
    fkmap = {fk.parent.name: fk.ondelete for fk in RequestChainStep.__table__.foreign_keys}
    for col in ("sender_client_id", "receiver_client_id", "account_owner_client_id"):
        assert fkmap.get(col) == "SET NULL", f"{col} should be SET NULL, got {fkmap.get(col)}"


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
        "folder_path",
        "display_order",
        "http_method_snapshot",
        "url_snapshot",
        "headers_snapshot",
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
        # Destination folder from the «Сохранить в папку» selector.
        "folder_path": json.dumps(["Проект", "Релиз"], ensure_ascii=False),
    }
    response = await templates_reg.htmx_fill_save(
        template_id=tpl_id,
        request=cast(Any, _FakeFormRequest(form)),
        templates=cast(Any, _FakeRenderer()),
        session=cast(Any, object()),
        # None = no group restriction, so the visibility guard is skipped in this
        # unit test (its concern is the no-rerender snapshot, not access control).
        group_ids=cast(Any, None),
    )

    assert captured["render_calls"] == 0, "save must not re-run _render_fill"
    kwargs = captured["save_kwargs"]
    assert kwargs is not None
    assert kwargs["rendered"] == '{"name": "FROZEN VALUE"}'
    assert kwargs["changed"] == ["/name"]
    assert kwargs["unresolved"] == ["sender.unknown"]
    assert kwargs["folder_path"] == ["Проект", "Релиз"]
    # Role IDs are still parsed from the form (for FK columns).
    assert kwargs["fill_request"].sender_client_id == sender_id
    assert response.status_code == 204
    # Redirect lands in the workspace with the saved item's panel open.
    assert response.headers["HX-Redirect"].startswith("/templater/filled-templates?open=")
    assert response.headers["HX-Redirect"].endswith("&saved=1")


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
    # rather than calling it (calling it would need a real AsyncSession),
    # reusing the repo's own defer list so the mirror can't drift from it.
    from sqlalchemy import or_, select

    from app.db.models import FilledTemplate as FT
    from app.repositories.filled_template import _LIST_DEFERS

    stmt = (
        select(FT)
        .options(*_LIST_DEFERS)
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
    assert "headers_snapshot" not in sql, "headers_snapshot must be deferred"
    assert "limit" in sql

    # Confirm a search-mode query keeps both invariants.
    term = "abc"
    like = f"%{term}%"
    stmt_search = stmt.where(or_(FT.name.ilike(like), FT.template_name_snapshot.ilike(like)))
    sql_search = str(stmt_search.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "filled_content" not in sql_search
    assert "limit" in sql_search

    # list_by_template (the «связанные заполненные шаблоны» panel) renders
    # only name/date links — it must defer the same heavy columns.
    stmt_by_template = (
        select(FT)
        .options(*_LIST_DEFERS)
        .where(FT.message_template_id.is_not(None))
        .order_by(FT.created_at.desc())
        .limit(DEFAULT_LIST_LIMIT)
    )
    sql_by_template = str(
        stmt_by_template.compile(compile_kwargs={"literal_binds": True})
    ).lower()
    assert "filled_content" not in sql_by_template
    assert "headers_snapshot" not in sql_by_template


# ---------------------------------------------------------------------------
# Project snapshots: filled templates capture project name/color at fill time.
# ---------------------------------------------------------------------------

from app.schemas.template import TemplateFillRequest  # noqa: E402
from app.services.filled_templates import FilledTemplateService  # noqa: E402


class _SnapshotSession:
    def __init__(self, *, max_display_order: int = -1) -> None:
        self.added: list[Any] = []
        # What the ``next_display_order`` aggregate "finds" in the target
        # folder; -1 mirrors an empty folder (COALESCE(MAX(...), -1)).
        self._max_display_order = max_display_order

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def execute(self, stmt: Any) -> Any:
        value = self._max_display_order
        return SimpleNamespace(scalar_one=lambda: value)


@pytest.mark.asyncio
async def test_save_from_fill_snapshots_project_name_and_color() -> None:
    session = _SnapshotSession()
    template = SimpleNamespace(
        id=uuid.uuid4(),
        name="A2A",
        format="json",
        project=SimpleNamespace(name="Альфа", color="#112233"),
    )
    saved = await FilledTemplateService(cast(Any, session)).save_from_fill(
        template=cast(Any, template),
        fill_request=TemplateFillRequest(),
        rendered="{}",
        changed=[],
        unresolved=[],
    )
    assert saved.project_name_snapshot == "Альфа"
    assert saved.project_color_snapshot == "#112233"


@pytest.mark.asyncio
async def test_save_from_fill_tolerates_template_without_project() -> None:
    # Test doubles (and defensive paths) may not carry the relationship at all.
    session = _SnapshotSession()
    template = SimpleNamespace(id=uuid.uuid4(), name="A2A", format="json")
    saved = await FilledTemplateService(cast(Any, session)).save_from_fill(
        template=cast(Any, template),
        fill_request=TemplateFillRequest(),
        rendered="{}",
        changed=[],
        unresolved=[],
    )
    assert saved.project_name_snapshot == ""
    assert saved.project_color_snapshot == ""


# ---------------------------------------------------------------------------
# Execution snapshots + folder placement: filled templates capture the HTTP
# method/url/headers of the source template (for the future "send request"
# feature) and land in the folder picked at save time.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_from_fill_snapshots_http_request_and_folder() -> None:
    session = _SnapshotSession()
    template = SimpleNamespace(
        id=uuid.uuid4(),
        name="A2A",
        format="json",
        http_method="POST",
        url="https://api.example.com/v1/transfer",
        headers=[{"key": "RqUID", "value": "{{rqUID}}", "mode": "dynamic"}],
    )
    saved = await FilledTemplateService(cast(Any, session)).save_from_fill(
        template=cast(Any, template),
        fill_request=TemplateFillRequest(),
        rendered="{}",
        changed=[],
        unresolved=[],
        folder_path=["Проект", "Релиз", "Фича"],
    )
    assert saved.folder_path == ["Проект", "Релиз", "Фича"]
    assert saved.http_method_snapshot == "POST"
    assert saved.url_snapshot == "https://api.example.com/v1/transfer"
    assert saved.headers_snapshot == [
        {"key": "RqUID", "value": "{{rqUID}}", "mode": "dynamic"}
    ]
    assert saved.display_order == 0  # first row in an empty folder


@pytest.mark.asyncio
async def test_save_from_fill_appends_after_ordered_siblings() -> None:
    # A folder whose siblings were manually ordered up to 4: the new row must
    # land *after* them (display_order=5), not at 0 where it would jump to the
    # top and collide with the existing first item.
    session = _SnapshotSession(max_display_order=4)
    template = SimpleNamespace(id=uuid.uuid4(), name="A2A", format="json")
    saved = await FilledTemplateService(cast(Any, session)).save_from_fill(
        template=cast(Any, template),
        fill_request=TemplateFillRequest(),
        rendered="{}",
        changed=[],
        unresolved=[],
        folder_path=["Проект"],
    )
    assert saved.display_order == 5


@pytest.mark.asyncio
async def test_save_from_fill_defaults_when_http_fields_and_folder_absent() -> None:
    session = _SnapshotSession()
    template = SimpleNamespace(id=uuid.uuid4(), name="A2A", format="json")
    saved = await FilledTemplateService(cast(Any, session)).save_from_fill(
        template=cast(Any, template),
        fill_request=TemplateFillRequest(),
        rendered="{}",
        changed=[],
        unresolved=[],
    )
    assert saved.folder_path == []
    assert saved.http_method_snapshot == ""
    assert saved.url_snapshot == ""
    assert saved.headers_snapshot == []
    assert saved.display_order == 0
