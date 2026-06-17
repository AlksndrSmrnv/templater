from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.export_import import (
    ExportImportService,
    _as_dict,
    _as_list,
    _safe_label,
    _validate_attributes_field,
    _validate_bool,
    _validate_object_field,
    _validate_optional_str,
    _validate_required_str,
    _validate_tags,
)


def _session() -> AsyncSession:
    return cast(AsyncSession, None)


def test_as_dict_passes_through_dict() -> None:
    assert _as_dict({"values": ["USD", "EUR"]}) == {"values": ["USD", "EUR"]}


def test_as_dict_coerces_everything_else_to_empty() -> None:
    # Lenient helper: anything that isn't a dict becomes {}.
    for value in (None, "string", '{"a": 1}', [1, 2, 3], 42):
        assert _as_dict(value) == {}


def test_as_list_only_passes_lists() -> None:
    assert _as_list([1, 2]) == [1, 2]
    assert _as_list(None) == []
    assert _as_list({"a": 1}) == []
    assert _as_list("string") == []


def test_safe_label_handles_non_dict_rows() -> None:
    assert _safe_label({"name": "X", "id": "1"}, "name", "id") == "X"
    assert _safe_label({"id": "1"}, "name", "id") == "1"
    assert _safe_label("not a dict", "name") == "<?>"
    assert _safe_label(None, "name") == "<?>"


def test_validate_bool_strict() -> None:
    # Real bools pass through; absent → default.
    assert _validate_bool(True, False) == (True, None)
    assert _validate_bool(None, True) == (True, None)
    # The string "false" must NOT become True (bool("false") is True).
    val, err = _validate_bool("false", False)
    assert err is not None
    val, err = _validate_bool(1, False)
    assert err is not None


def test_validate_required_str() -> None:
    assert _validate_required_str("ok", 128) == ("ok", None)
    assert _validate_required_str(None, 128)[1] is not None
    assert _validate_required_str("", 128)[1] is not None
    assert _validate_required_str("   ", 128)[1] is not None
    assert _validate_required_str(123, 128)[1] is not None
    assert _validate_required_str("x" * 200, 128)[1] is not None


def test_validate_optional_str() -> None:
    assert _validate_optional_str(None) == (None, None)  # absent
    assert _validate_optional_str("text") == ("text", None)
    assert _validate_optional_str("") == ("", None)  # empty allowed for optional
    assert _validate_optional_str(42)[1] is not None


def test_validate_object_field() -> None:
    assert _validate_object_field(None) == ({}, None)  # absent / null → {}
    assert _validate_object_field({"a": 1}) == ({"a": 1}, None)
    # non-object values must error, not silently become {}
    bad_values: tuple[Any, ...] = ("bad", [], 42, True)
    for bad in bad_values:
        obj, err = _validate_object_field(bad)
        assert obj is None
        assert err is not None


@pytest.mark.asyncio
async def test_import_rejects_non_dict_top_level() -> None:
    """A malformed file (top-level JSON array / string) must return an error
    summary, not raise — /api/import would otherwise 500."""

    svc = ExportImportService(session=_session())
    for bad in (["a", "b"], "just a string", 42):
        summary = await svc.import_package(bad, policy="skip")
        assert summary.errors, f"expected error for {bad!r}"
        assert all(v == 0 for v in summary.created.values())
        assert all(v == 0 for v in summary.updated.values())


def test_validate_tags_accepts_list_of_strings() -> None:
    tags, err = _validate_tags(["vip", "test"])
    assert err is None
    assert tags == ["vip", "test"]


def test_validate_tags_none_means_absent() -> None:
    tags, err = _validate_tags(None)
    assert err is None
    assert tags is None  # caller keeps the existing value


def test_validate_tags_rejects_string() -> None:
    # list("vip") would explode into ["v","i","p"] — must be rejected instead.
    tags, err = _validate_tags("vip")
    assert tags is None
    assert err is not None


def test_validate_tags_rejects_non_string_items() -> None:
    tags, err = _validate_tags(["ok", 123])
    assert tags is None
    assert err is not None


def test_validate_attributes_field_absent_is_empty() -> None:
    attrs, err = _validate_attributes_field(None)
    assert err is None
    assert attrs == {}


def test_validate_attributes_field_passes_dict() -> None:
    attrs, err = _validate_attributes_field({"fullName": "X"})
    assert err is None
    assert attrs == {"fullName": "X"}


def test_validate_attributes_field_rejects_string_and_list() -> None:
    # _as_dict would silently coerce these to {} — losing/wiping attributes.
    for bad in ("oops", [1, 2], 42):
        attrs, err = _validate_attributes_field(bad)
        assert attrs is None
        assert err is not None


@pytest.mark.asyncio
async def test_import_reports_wrong_shaped_sections() -> None:
    """A file where a list section is given as an object must surface an
    explicit error rather than looking like a successful empty import."""

    svc = ExportImportService(session=_session())
    package = {
        "clients": {"oops": "object instead of list"},
        "templates": "a string",
        "attribute_schema": {"also": "wrong"},
    }
    summary = await svc.import_package(package, policy="skip")
    joined = " | ".join(summary.errors)
    assert "clients" in joined
    assert "templates" in joined
    assert "attribute_schema" in joined


# ---------- Collection metadata round-trip (review fix) ----------

import uuid  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from app.db.models import Collection, MessageTemplate  # noqa: E402


class _EmptyResult:
    def scalars(self) -> _EmptyResult:
        return self

    def all(self) -> list[Any]:
        return []

    def scalar_one_or_none(self) -> None:
        # Project-by-name lookup resolves to "missing" → import creates the row.
        return None


class _RoundTripSession:
    """In-memory AsyncSession stand-in: ``add`` makes objects visible to later
    ``get`` (by type+id), so import_package can resolve collection_id FKs without
    a real database. ``execute`` returns an empty result — the round-trip package
    only carries collections + templates (no attribute/reference selects)."""

    def __init__(self) -> None:
        self.store: dict[tuple[type, Any], Any] = {}
        self.added: list[Any] = []

    async def get(self, model: type, ident: Any) -> Any:
        return self.store.get((model, ident))

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        ident = getattr(obj, "id", None)
        self.store[(type(obj), ident)] = obj

    async def execute(self, *args: Any, **kwargs: Any) -> _EmptyResult:
        return _EmptyResult()

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@pytest.mark.asyncio
async def test_template_roundtrip_preserves_collection_metadata() -> None:
    coll_id, t1, t2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    parsable = SimpleNamespace(
        id=t1, name="A2A", description="перевод", format="json",
        content='{"amount": 100}', original_content='{"amount": 100}',
        llm_meta={"import_status": "imported"}, placeholders=[],
        collection_id=coll_id, folder_path=["Transfers"],
        headers=[{"key": "RqUID", "value": "{{rqUID}}", "mode": "dynamic", "original": "x", "disabled": False}],
        http_method="POST", url="https://api/transfer", display_order=0,
    )
    unparsed = SimpleNamespace(
        id=t2, name="Health", description="", format="json",
        content="", original_content="", llm_meta={"import_status": "unparsed"},
        placeholders=[], collection_id=coll_id, folder_path=[],
        headers=[{"key": "Accept", "value": "application/json", "mode": "literal", "original": "application/json", "disabled": False}],
        http_method="GET", url="https://api/health", display_order=1,
    )
    coll = SimpleNamespace(
        id=coll_id, name="Demo", description="", source="postman",
        source_format="v2.1.0", variables=[],
        # "Reports/Daily" holds no templates — it only survives the round-trip if
        # Collection.folders is exported and re-imported.
        folders=[["Transfers"], ["Reports"], ["Reports", "Daily"]],
    )

    package = {
        "version": 2,
        "collections": [ExportImportService._dump_collection(coll)],
        "templates": [
            ExportImportService._dump_template(parsable),
            ExportImportService._dump_template(unparsed),
        ],
    }

    session = cast(Any, _RoundTripSession())
    summary = await ExportImportService(session).import_package(package, policy="skip")

    assert summary.errors == []
    assert summary.created["collections"] == 1
    assert summary.created["templates"] == 2

    templates = {t.name: t for t in session.added if isinstance(t, MessageTemplate)}
    a2a = templates["A2A"]
    assert a2a.collection_id == coll_id
    assert a2a.folder_path == ["Transfers"]
    assert a2a.http_method == "POST"
    assert a2a.url == "https://api/transfer"
    assert a2a.headers[0]["mode"] == "dynamic"

    health = templates["Health"]
    # Unparsed body imported verbatim (empty) — no "non-empty content" error.
    assert health.content == ""
    assert health.collection_id == coll_id
    assert health.http_method == "GET"

    collections = [c for c in session.added if isinstance(c, Collection)]
    assert len(collections) == 1 and collections[0].source_format == "v2.1.0"
    # Empty folders preserved through export → import.
    assert ["Reports", "Daily"] in collections[0].folders


@pytest.mark.asyncio
async def test_import_overwrite_clears_stale_llm_debug() -> None:
    # Backups omit llm_debug by design; overwriting an existing template must not
    # leave the previous local template's prompts/response attached to the new
    # restored content.
    tid = uuid.uuid4()
    existing = SimpleNamespace(
        id=tid, name="Old", description="", format="json",
        content='{"a":"x"}', original_content='{"a":"x"}',
        llm_meta={"import_status": "imported"}, placeholders=[],
        llm_debug={"system_prompt": "old", "user_prompt": "old", "response_text": "old"},
        collection_id=None, folder_path=[], headers=[],
        http_method="", url="", display_order=0,
    )
    session = _RoundTripSession()
    session.store[(MessageTemplate, tid)] = existing

    package = {
        "version": 2,
        "templates": [
            {
                "id": str(tid),
                "name": "New",
                "format": "json",
                "content": '{"b":"y"}',
                "original_content": '{"b":"y"}',
                "llm_meta": {"import_status": "imported"},
                "placeholders": [],
            }
        ],
    }

    summary = await ExportImportService(cast(Any, session)).import_package(
        package, policy="overwrite"
    )

    assert summary.errors == []
    assert existing.content == '{"b":"y"}'
    assert existing.llm_debug is None


@pytest.mark.asyncio
async def test_import_reports_wrong_shaped_collections_section() -> None:
    svc = ExportImportService(cast(AsyncSession, _RoundTripSession()))
    summary = await svc.import_package({"collections": "nope"}, policy="skip")
    joined = " ".join(summary.errors)
    assert "collections" in joined


@pytest.mark.asyncio
async def test_import_keeps_nonempty_unparsable_body_verbatim() -> None:
    # Mirrors the Postman parser's parsable=False bodies: a non-empty body that
    # does not parse must still import (verbatim), forced to "unparsed" with no
    # placeholders — not rejected as invalid JSON.
    tid = uuid.uuid4()
    package = {
        "version": 2,
        "templates": [
            {
                "id": str(tid),
                "name": "Broken",
                "format": "json",
                "content": "{not json",
                "original_content": "{not json",
                "llm_meta": {"import_status": "imported"},
                "placeholders": [{"location": "/a", "mode": "literal", "value": "x"}],
                "http_method": "POST",
                "url": "https://api/broken",
            }
        ],
    }
    session = cast(Any, _RoundTripSession())
    summary = await ExportImportService(session).import_package(package, policy="skip")

    assert summary.errors == []
    assert summary.created["templates"] == 1
    template = next(o for o in session.added if isinstance(o, MessageTemplate))
    assert template.content == "{not json"
    assert template.original_content == "{not json"
    assert template.placeholders == []
    assert template.llm_meta["import_status"] == "unparsed"
    assert template.http_method == "POST"


# ---------- Projects: exported by name, restored via get-or-create ----------

from app.db.models import Project  # noqa: E402
from app.services.projects import DEFAULT_PROJECT_NAME  # noqa: E402


def _project_template(name: str = "T", **overrides: Any) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(), name=name, description="", format="json",
        content='{"a": 1}', original_content='{"a": 1}',
        llm_meta={}, placeholders=[], collection_id=None, folder_path=[],
        headers=[], http_method="POST", url="", display_order=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_dump_template_includes_project_name_and_color() -> None:
    template = _project_template(project=SimpleNamespace(name="Альфа", color="#112233"))
    dump = ExportImportService._dump_template(cast(Any, template))
    assert dump["project_name"] == "Альфа"
    assert dump["project_color"] == "#112233"

    # Doubles without the relationship degrade to None, not AttributeError.
    bare = ExportImportService._dump_template(cast(Any, _project_template()))
    assert bare["project_name"] is None


@pytest.mark.asyncio
async def test_import_creates_named_project_and_links_templates() -> None:
    package = {
        "version": 2,
        "templates": [
            ExportImportService._dump_template(
                cast(Any, _project_template(project=SimpleNamespace(name="Альфа", color="#112233")))
            ),
            ExportImportService._dump_template(
                cast(Any, _project_template(name="T2", project=SimpleNamespace(name="Альфа", color="#112233")))
            ),
        ],
    }
    session = _RoundTripSession()
    summary = await ExportImportService(cast(Any, session)).import_package(package, policy="skip")

    assert summary.errors == []
    projects = [p for p in session.added if isinstance(p, Project)]
    # Both templates share one (cached) project row.
    assert len(projects) == 1
    assert projects[0].name == "Альфа"
    assert projects[0].color == "#112233"
    templates = [t for t in session.added if isinstance(t, MessageTemplate)]
    assert all(t.project_id == projects[0].id for t in templates)


@pytest.mark.asyncio
async def test_import_without_project_name_falls_back_to_default_project() -> None:
    # Old v2 packages predate projects — their templates land in «Без проекта»,
    # mirroring the migration semantics. A bad color falls back to the default.
    raw = ExportImportService._dump_template(cast(Any, _project_template()))
    assert raw["project_name"] is None
    package = {"version": 2, "templates": [raw]}
    session = _RoundTripSession()
    summary = await ExportImportService(cast(Any, session)).import_package(package, policy="skip")

    assert summary.errors == []
    projects = [p for p in session.added if isinstance(p, Project)]
    assert len(projects) == 1
    assert projects[0].name == DEFAULT_PROJECT_NAME
    templates = [t for t in session.added if isinstance(t, MessageTemplate)]
    assert templates[0].project_id == projects[0].id


class _KnownProjectSession(_RoundTripSession):
    """Round-trip session whose project-by-name lookup resolves to a fixed row."""

    def __init__(self, project: Project) -> None:
        super().__init__()
        self._project = project

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        project = self._project

        class _Result(_EmptyResult):
            def scalar_one_or_none(self) -> Project:
                return project

        return _Result()


@pytest.mark.asyncio
async def test_import_without_permission_errors_rows_needing_new_project() -> None:
    # allow_project_creation=False: a row whose project doesn't exist fails with
    # a row-level error instead of creating the project behind the lock.
    package = {
        "version": 2,
        "templates": [
            ExportImportService._dump_template(
                cast(Any, _project_template(project=SimpleNamespace(name="Новый", color="#112233")))
            )
        ],
    }
    session = _RoundTripSession()
    summary = await ExportImportService(cast(Any, session)).import_package(
        package, policy="skip", allow_project_creation=False
    )

    assert summary.created["templates"] == 0
    assert any("Новый" in err and "режиме редактирования" in err for err in summary.errors)
    assert [p for p in session.added if isinstance(p, Project)] == []


@pytest.mark.asyncio
async def test_import_without_permission_reuses_existing_project() -> None:
    existing = Project(id=uuid.uuid4(), name="Альфа", color="#112233")
    package = {
        "version": 2,
        "templates": [
            ExportImportService._dump_template(
                cast(Any, _project_template(project=SimpleNamespace(name="Альфа", color="#112233")))
            )
        ],
    }
    session = _KnownProjectSession(existing)
    summary = await ExportImportService(cast(Any, session)).import_package(
        package, policy="skip", allow_project_creation=False
    )

    assert summary.errors == []
    assert summary.created["templates"] == 1
    templates = [t for t in session.added if isinstance(t, MessageTemplate)]
    assert templates[0].project_id == existing.id
    # Reused, not recreated.
    assert [p for p in session.added if isinstance(p, Project)] == []


@pytest.mark.asyncio
async def test_import_without_permission_skips_existing_rows_without_error() -> None:
    # policy=skip + existing template id: the row is never written, so its
    # unknown project_name must not surface a permission error (previously the
    # route-level preflight produced a false 403 here).
    tid = uuid.uuid4()
    existing = SimpleNamespace(
        id=tid, name="Old", description="", format="json",
        content='{"a":"x"}', original_content='{"a":"x"}',
        llm_meta={}, placeholders=[], llm_debug=None,
        collection_id=None, folder_path=[], headers=[],
        http_method="", url="", display_order=0,
    )
    session = _RoundTripSession()
    session.store[(MessageTemplate, tid)] = existing

    package = {
        "version": 2,
        "templates": [
            {
                "id": str(tid),
                "name": "New",
                "format": "json",
                "content": '{"b":"y"}',
                "placeholders": [],
                "project_name": "Новый",
            }
        ],
    }
    summary = await ExportImportService(cast(Any, session)).import_package(
        package, policy="skip", allow_project_creation=False
    )

    assert summary.errors == []
    assert summary.skipped["templates"] == 1
    assert [p for p in session.added if isinstance(p, Project)] == []


# ---------- Access-group export/import gating ----------

from app.db.models import AccessGroup, Client  # noqa: E402


class _FakeExportRepo:
    """Models the visibility-filtered ``get_many``: a row is returned only when
    requested AND public (``group_id is None``) or in an unlocked group. Mirrors
    the SQL predicate so the service-level filtering can be tested without a DB."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.seen_visible: Any = "unset"

    async def get_many(self, ids: list[Any], *, visible_group_ids: Any = None) -> list[Any]:
        self.seen_visible = visible_group_ids
        wanted = set(ids)
        out = []
        for r in self.rows:
            if r.id not in wanted:
                continue
            gid = getattr(r, "group_id", None)
            if visible_group_ids is None or gid is None or gid in visible_group_ids:
                out.append(r)
        return out


class _FakeAttrRepo:
    async def list_all(self) -> list[Any]:
        return []


def _client_ns(group_id: Any = None, group: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), description="", tags=[], attributes={},
        group_id=group_id, group=group,
    )


@pytest.mark.asyncio
async def test_export_excludes_clients_outside_unlocked_groups() -> None:
    group_a = uuid.uuid4()
    group_b = uuid.uuid4()
    public = _client_ns()
    in_a = _client_ns(group_id=group_a, group=SimpleNamespace(name="A", color="#111111"))
    in_b = _client_ns(group_id=group_b, group=SimpleNamespace(name="B", color="#222222"))

    svc = ExportImportService(cast(Any, _RoundTripSession()))
    svc.clients = cast(Any, _FakeExportRepo([public, in_a, in_b]))
    svc.accounts = cast(Any, _FakeExportRepo([]))
    svc.cards = cast(Any, _FakeExportRepo([]))
    svc.attrs = cast(Any, _FakeAttrRepo())

    from app.schemas.exchange import ExportRequest

    req = ExportRequest(clients=[public.id, in_a.id, in_b.id])
    package = await svc.export(req, visible_group_ids={group_a})

    exported_ids = {c["id"] for c in package.clients}
    assert exported_ids == {str(public.id), str(in_a.id)}  # group B is hidden
    assert str(in_b.id) not in exported_ids
    # The visibility set was actually threaded to the repository.
    assert svc.clients.seen_visible == {group_a}


def test_dump_client_carries_group_name_and_color_not_hash() -> None:
    c = _client_ns(group_id=uuid.uuid4(), group=SimpleNamespace(name="QA", color="#7E57C2"))
    dump = ExportImportService._dump_client(cast(Any, c))
    assert dump["group_name"] == "QA"
    assert dump["group_color"] == "#7E57C2"
    assert "password_hash" not in dump
    # A public client carries explicit nulls (key present, no group).
    public = ExportImportService._dump_client(cast(Any, _client_ns()))
    assert public["group_name"] is None and public["group_color"] is None


class _KnownGroupSession(_RoundTripSession):
    """Round-trip session whose group-by-name lookup resolves to a fixed row."""

    def __init__(self, group: Any) -> None:
        super().__init__()
        self._group = group

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        group = self._group

        class _Result(_EmptyResult):
            def scalar_one_or_none(self) -> Any:
                return group

        return _Result()


@pytest.mark.asyncio
async def test_import_client_is_public_when_group_name_unknown() -> None:
    cid = uuid.uuid4()
    package = {
        "version": 3,
        "clients": [{"id": str(cid), "group_name": "Несуществующая"}],
    }
    session = _RoundTripSession()  # group lookup resolves to None
    summary = await ExportImportService(cast(Any, session)).import_package(package, policy="skip")

    assert summary.errors == []
    assert summary.created["clients"] == 1
    created = next(o for o in session.added if isinstance(o, Client))
    assert created.group_id is None  # lands public


@pytest.mark.asyncio
async def test_import_client_reassociates_to_existing_same_named_group() -> None:
    cid = uuid.uuid4()
    group = AccessGroup(id=uuid.uuid4(), name="QA", color="#7E57C2", password_hash="x")
    package = {
        "version": 3,
        "clients": [{"id": str(cid), "group_name": "QA"}],
    }
    session = _KnownGroupSession(group)
    summary = await ExportImportService(cast(Any, session)).import_package(package, policy="skip")

    assert summary.errors == []
    created = next(o for o in session.added if isinstance(o, Client))
    assert created.group_id == group.id


@pytest.mark.asyncio
async def test_overwrite_never_exposes_a_private_client() -> None:
    cid = uuid.uuid4()
    private_group = uuid.uuid4()
    existing = SimpleNamespace(
        id=cid, description="old", tags=[], attributes={}, group_id=private_group
    )
    session = _RoundTripSession()  # unknown group → resolve_group_id returns None
    session.store[(Client, cid)] = existing
    # File has no resolvable group; overwrite must NOT move the client to public.
    package = {"version": 3, "clients": [{"id": str(cid), "group_name": None}]}
    summary = await ExportImportService(cast(Any, session)).import_package(
        package, policy="overwrite"
    )

    assert summary.errors == []
    assert summary.updated["clients"] == 1
    assert existing.group_id == private_group  # protection preserved
