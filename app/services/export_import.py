"""Selective export with dependency closure, and import with conflict policy."""

from __future__ import annotations

import re
import uuid
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    DATA_ENTITY_TYPES,
    Account,
    AttributeDefinition,
    Card,
    Client,
    Collection,
    MessageTemplate,
)
from app.repositories.access_group import AccessGroupRepository
from app.repositories.attribute import AttributeDefinitionRepository
from app.repositories.entity import (
    AccountRepository,
    CardRepository,
    ClientRepository,
)
from app.repositories.template import TemplateRepository
from app.schemas.attribute import ALLOWED_TYPES
from app.schemas.exchange import ExportPackage, ExportRequest, ImportSummary
from app.schemas.project import COLOR_PATTERN
from app.services.attribute_schema import AttributeSchemaService
from app.services.projects import (
    DEFAULT_PROJECT_COLOR,
    DEFAULT_PROJECT_NAME,
    ProjectService,
)
from app.services.templates import normalize_placeholders
from app.utils.errors import ValidationFailed


def _as_dict(value: Any) -> dict[str, Any]:
    """Lenient coercion to a dict: a dict passes through, anything else → ``{}``.

    Used for non-critical object fields (``options``, ``llm_meta`` scanning)
    where a malformed value should degrade quietly. Fields where a wrong shape
    must surface an error use :func:`_validate_object_field` /
    :func:`_validate_attributes_field` instead.
    """

    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    """Return ``value`` if it's a list, else an empty list. Guards against a
    section of the import file being the wrong JSON shape."""

    return value if isinstance(value, list) else []


def _collect_uuids(items: Any, *keys: str) -> set[uuid.UUID]:
    """Best-effort UUID collection from a list of import rows.

    Used to prefetch/warm rows before the per-row import loops. Malformed or
    missing values are ignored here — the loop itself re-parses and reports them.
    """

    out: set[uuid.UUID] = set()
    for raw in _as_list(items):
        if not isinstance(raw, dict):
            continue
        for key in keys:
            val = raw.get(key)
            if not val:
                continue
            try:
                out.add(uuid.UUID(str(val)))
            except (ValueError, AttributeError, TypeError):
                continue
    return out


def _safe_label(raw: Any, *keys: str) -> str:
    """Best-effort human label for an import row that may not even be a dict."""

    if isinstance(raw, dict):
        for k in keys:
            v = raw.get(k)
            if v:
                return str(v)
    return "<?>"


def _validate_tags(value: Any) -> tuple[list[str] | None, str | None]:
    """Validate an imported ``tags`` value.

    Returns ``(tags, error)``. ``tags is None`` with no error means the field
    was absent (caller should keep the existing value). A wrong shape — e.g. the
    string ``"vip"``, which ``list(...)`` would explode into ``["v","i","p"]`` —
    yields an error instead of silently corrupting the row.
    """

    if value is None:
        return None, None
    if not isinstance(value, list):
        return None, "tags должен быть списком строк"
    if not all(isinstance(t, str) for t in value):
        return None, "tags должен содержать только строки"
    return list(value), None


def _validate_object_field(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Strictly validate an imported JSON-object field.

    Unlike :func:`_as_dict` (which quietly degrades to ``{}``), this requires a
    real object: absent / null → ``{}``, a dict → itself, anything else
    (string, list, number) → error. Silent coercion to ``{}`` would drop data
    on create and could wipe existing data on overwrite.
    """

    if value is None:
        return {}, None
    if isinstance(value, dict):
        return value, None
    return None, "должно быть объектом"


def _validate_attributes_field(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Strict validation of an imported ``attributes`` value (see
    :func:`_validate_object_field`)."""

    obj, err = _validate_object_field(value)
    if err:
        return None, "attributes должен быть объектом"
    return obj, None


def _validate_bool(value: Any, default: bool) -> tuple[bool, str | None]:
    """Validate an imported boolean against the strict shape CRUD expects.

    Absent / null → ``default``. A real JSON bool → itself. Anything else
    (notably the string ``"false"``, which ``bool(...)`` would turn into
    ``True``) → error.
    """

    if value is None:
        return default, None
    if isinstance(value, bool):
        return value, None
    return default, "ожидалось true/false"


def _validate_required_str(value: Any, max_length: int) -> tuple[str | None, str | None]:
    """Validate a required string field (non-empty, length-bounded) — mirrors the
    Pydantic constraints used on the CRUD path."""

    if value is None:
        return None, "обязательное поле"
    if not isinstance(value, str):
        return None, "должно быть строкой"
    if not value.strip():
        return None, "не может быть пустым"
    if len(value) > max_length:
        return None, f"длина превышает {max_length}"
    return value, None


def _validate_optional_str(value: Any) -> tuple[str | None, str | None]:
    """Validate an optional string field. ``(None, None)`` means the field was
    absent (caller keeps the existing value / default)."""

    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "должно быть строкой"
    return value, None


class ExportImportService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.attrs = AttributeDefinitionRepository(session)
        self.clients = ClientRepository(session)
        self.accounts = AccountRepository(session)
        self.cards = CardRepository(session)
        self.templates = TemplateRepository(session)
        self.groups = AccessGroupRepository(session)

    async def export(
        self, req: ExportRequest, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> ExportPackage:
        # gather: cards -> their accounts -> their clients. Every fetch is
        # filtered to ``visible_group_ids`` (public + unlocked) so a crafted
        # request can't pull private data by id. Accounts/cards inherit their
        # client's group, so a card visible here always resolves to a visible
        # account and client — the closure never widens past what's allowed.
        card_objs = await self.cards.get_many(req.cards, visible_group_ids=visible_group_ids)
        account_ids = set(req.accounts) | {c.account_id for c in card_objs}
        account_objs = await self.accounts.get_many(
            list(account_ids), visible_group_ids=visible_group_ids
        )
        client_ids = set(req.clients) | {a.client_id for a in account_objs}
        client_objs = await self.clients.get_many(
            list(client_ids), visible_group_ids=visible_group_ids
        )

        all_defs = await self.attrs.list_all()
        templates = await self.templates.get_many(req.templates)

        # Pull in the collections those templates belong to so restore keeps the
        # workspace tree intact (template.collection_id is a FK to collections).
        collection_ids = {t.collection_id for t in templates if t.collection_id}
        collection_objs: list[Collection] = []
        if collection_ids:
            stmt = select(Collection).where(Collection.id.in_(list(collection_ids)))
            collection_objs = list((await self.session.execute(stmt)).scalars().all())

        return ExportPackage(
            version=2,
            attribute_schema=[self._dump_attr(a) for a in all_defs],
            clients=[self._dump_client(c) for c in client_objs],
            accounts=[self._dump_account(a) for a in account_objs],
            cards=[self._dump_card(c) for c in card_objs],
            collections=[self._dump_collection(c) for c in collection_objs],
            templates=[self._dump_template(t) for t in templates],
        )

    async def import_package(
        self,
        package: Any,
        *,
        policy: str = "skip",
        allow_project_creation: bool = True,
    ) -> ImportSummary:
        if policy not in ("skip", "overwrite", "fail"):
            policy = "skip"
        counter_keys = (
            "attribute_schema",
            "clients",
            "accounts",
            "cards",
            "collections",
            "templates",
        )
        created = {k: 0 for k in counter_keys}
        updated = {k: 0 for k in counter_keys}
        skipped = {k: 0 for k in counter_keys}
        errors: list[str] = []
        schema_svc = AttributeSchemaService(self.session)

        def _zeroed_summary() -> ImportSummary:
            zero = {k: 0 for k in counter_keys}
            return ImportSummary(
                created=dict(zero), updated=dict(zero), skipped=dict(zero), errors=errors
            )

        # The uploaded file may contain any JSON value. A non-object top level
        # would crash package.get(...) below — fail with a summary, not a 500.
        if not isinstance(package, dict):
            errors.append("Ожидался JSON-объект на верхнем уровне файла импорта")
            return _zeroed_summary()

        # Report every wrong-shaped section explicitly and abort. Otherwise a
        # file with e.g. ``clients: {...}`` would be silently treated as empty
        # and look like a successful zero-change import.
        shape_errors = False
        for section in ("attribute_schema", "clients", "accounts", "cards", "collections", "templates"):
            value = package.get(section)
            if value is not None and not isinstance(value, list):
                errors.append(f"Секция '{section}' должна быть списком")
                shape_errors = True
        if shape_errors:
            return _zeroed_summary()

        # Attributes only ever belong to the core data entities now.
        all_attr_types = set(DATA_ENTITY_TYPES)

        def conflict(kind: str, raw: dict[str, Any]) -> bool:
            """Record a conflict according to ``policy``. Returns True if caller
            should stop processing this row (skip / fail), False if caller may
            still apply changes (overwrite)."""

            if policy == "overwrite":
                return False
            if policy == "fail":
                errors.append(f"{kind} {raw.get('id') or raw.get('name') or raw.get('code')}: уже существует")
                skipped[kind] += 1
                return True
            skipped[kind] += 1
            return True

        async def safe_flush(stage: str) -> bool:
            """Flush pending changes; on DB error roll back and record it.

            Returns True when the flush succeeded. A failed flush leaves the
            session unusable, so callers must abort the whole import.
            """

            try:
                await self.session.flush()
                return True
            except (IntegrityError, SQLAlchemyError) as exc:
                await self.session.rollback()
                errors.append(f"flush failed ({stage}): {exc.orig if hasattr(exc, 'orig') else exc}")
                return False

        # ---- attribute_schema (validated, never trusts the file blindly) ----
        seen_attr_keys: set[tuple[str, str]] = set()
        for raw in _as_list(package.get("attribute_schema")):
            if not isinstance(raw, dict):
                errors.append("attribute_schema: запись не является объектом")
                continue
            raw = cast(dict[str, Any], raw)
            try:
                # entity_type + name are needed just to locate the row, so they
                # are validated up front regardless of policy.
                entity_type_raw = raw.get("entity_type")
                raw_name = raw.get("name")
                if not isinstance(entity_type_raw, str) or entity_type_raw not in all_attr_types:
                    errors.append(f"attribute_schema {raw_name}: неизвестный entity_type '{entity_type_raw}'")
                    continue
                entity_type = entity_type_raw
                name, name_err = _validate_required_str(raw_name, 128)
                if name_err or name is None:
                    errors.append(f"attribute_schema {entity_type}: name {name_err}")
                    continue
                key = (entity_type, name)
                if key in seen_attr_keys:
                    errors.append(f"attribute_schema {entity_type}/{name}: дубликат в файле")
                    continue
                seen_attr_keys.add(key)

                existing_attr = await self.attrs.get_by_name(entity_type, name)
                # skip/fail on an existing row: leave it untouched, and don't
                # validate the (write-only) payload fields — a stale file with a
                # bad data_type shouldn't error for a row we won't write.
                if existing_attr is not None and conflict("attribute_schema", raw):
                    continue

                # Creating or overwriting — validate the payload now. Every
                # field is parsed into a local first; only once all of them
                # succeed do we touch the ORM object. A mid-way parse failure
                # must not leave a half-updated row dirty for the final commit.

                # data_type is immutable: turning an existing enum attribute
                # into e.g. a string would orphan its options and break the
                # already-stored data. Absent on overwrite → keep existing.
                raw_data_type = raw.get("data_type")
                if raw_data_type is None:
                    data_type = existing_attr.data_type if existing_attr is not None else "string"
                elif isinstance(raw_data_type, str):
                    data_type = raw_data_type
                else:
                    errors.append(f"attribute_schema {entity_type}/{name}: data_type должен быть строкой")
                    continue
                if data_type not in ALLOWED_TYPES:
                    errors.append(f"attribute_schema {entity_type}/{name}: неизвестный тип '{data_type}'")
                    continue
                if existing_attr is not None and data_type != existing_attr.data_type:
                    errors.append(
                        f"attribute_schema {entity_type}/{name}: data_type нельзя изменить "
                        f"({existing_attr.data_type} → {data_type})"
                    )
                    continue

                options = _as_dict(raw.get("options"))
                if data_type == "enum" and not (
                    isinstance(options.get("values"), list) and options.get("values")
                ):
                    errors.append(f"attribute_schema {entity_type}/{name}: enum без options.values")
                    continue

                # label: absent → fall back to name (or existing); present → validate.
                if raw.get("label") is None:
                    label = existing_attr.label if existing_attr is not None else name
                else:
                    parsed_label, label_err = _validate_required_str(raw.get("label"), 255)
                    if label_err or parsed_label is None:
                        errors.append(f"attribute_schema {entity_type}/{name}: label {label_err}")
                        continue
                    label = parsed_label

                description, desc_err = _validate_optional_str(raw.get("description"))
                if desc_err:
                    errors.append(f"attribute_schema {entity_type}/{name}: description {desc_err}")
                    continue
                if description is None:
                    description = existing_attr.description if existing_attr is not None else ""

                is_required, ir_err = _validate_bool(
                    raw.get("is_required"), existing_attr.is_required if existing_attr is not None else False
                )
                if ir_err:
                    errors.append(f"attribute_schema {entity_type}/{name}: is_required {ir_err}")
                    continue

                raw_display_order = raw.get("display_order")
                display_order_err = (
                    f"attribute_schema {entity_type}/{name}: display_order должен быть целым числом"
                )
                if raw_display_order is None:
                    display_order = existing_attr.display_order if existing_attr is not None else 0
                elif isinstance(raw_display_order, bool):
                    # bool is an int subclass — reject it explicitly.
                    errors.append(display_order_err)
                    continue
                elif isinstance(raw_display_order, int):
                    display_order = raw_display_order
                elif isinstance(raw_display_order, float):
                    # int(1.9) would silently truncate to 1 — reject non-integral.
                    if not raw_display_order.is_integer():
                        errors.append(display_order_err)
                        continue
                    display_order = int(raw_display_order)
                else:
                    # strings / other — accept only a strict integer representation.
                    try:
                        display_order = int(raw_display_order)
                    except (TypeError, ValueError):
                        errors.append(display_order_err)
                        continue

                if existing_attr is None:
                    self.session.add(AttributeDefinition(
                        entity_type=entity_type,
                        name=name,
                        label=label,
                        data_type=data_type,
                        is_required=is_required,
                        display_order=display_order,
                        description=description,
                        options=options,
                    ))
                    created["attribute_schema"] += 1
                else:
                    # all parsed — apply atomically (data_type unchanged by design)
                    existing_attr.label = label
                    existing_attr.is_required = is_required
                    existing_attr.display_order = display_order
                    existing_attr.description = description
                    existing_attr.options = options
                    updated["attribute_schema"] += 1
            except Exception as exc:
                errors.append(f"attribute_schema {raw.get('name')}: {exc}")

        # Flush so the schema below (and validate_attributes later) sees the
        # just-added definitions. autoflush=False means a SELECT won't do it.
        if not await safe_flush("attribute_schema"):
            return _zeroed_summary()

        async def _validated_entity_attrs(
            et: str, kind: str, label: str, raw: dict[str, Any]
        ) -> tuple[dict[str, Any] | None, str | None]:
            """Validate entity attributes like the CRUD path. Only called when
            we're actually about to write (create / overwrite)."""

            attrs_raw, attrs_err = _validate_attributes_field(raw.get("attributes"))
            if attrs_err:
                return None, f"{kind} {label}: {attrs_err}"
            assert attrs_raw is not None
            try:
                attrs = await schema_svc.validate_attributes(et, attrs_raw)
                return attrs, None
            except ValidationFailed as vexc:
                detail = "; ".join(vexc.details) if isinstance(vexc.details, list) else vexc.message
                return None, f"{kind} {label}: атрибуты не прошли проверку: {detail}"

        # clients, accounts, cards (in dependency order)
        seen_entity_ids: dict[str, set[str]] = {"clients": set(), "accounts": set(), "cards": set()}
        # IDs created earlier in *this* import. Such rows are pending (not yet
        # flushed), so session.get() can't see them — track them explicitly so
        # an account/card can reference a parent created in the same file.
        imported_new_ids: dict[str, set[str]] = {"clients": set(), "accounts": set()}
        # Warm the identity map for every row the loops below look up — the entities
        # themselves plus the parents they reference — so the per-row session.get() /
        # repo.get() calls resolve from the identity map instead of issuing N+1
        # queries. Parents created earlier in this same import aren't in the DB yet,
        # but the loop short-circuits those via ``imported_new_ids`` before calling get().
        warm_ids: dict[Any, set[uuid.UUID]] = {
            Client: _collect_uuids(package.get("clients"), "id")
            | _collect_uuids(package.get("accounts"), "client_id"),
            Account: _collect_uuids(package.get("accounts"), "id")
            | _collect_uuids(package.get("cards"), "account_id"),
            Card: _collect_uuids(package.get("cards"), "id"),
        }
        for warm_model, ids in warm_ids.items():
            if ids:
                await self.session.execute(select(warm_model).where(warm_model.id.in_(ids)))

        # Re-associate an imported client with a same-named access group on this
        # instance (by name — the export never carries the password). When no
        # such group exists the client lands public; groups are never created on
        # import, since they require a password we don't have.
        group_ids_by_name: dict[str, uuid.UUID | None] = {}

        async def resolve_group_id(raw_name: Any) -> uuid.UUID | None:
            if not isinstance(raw_name, str) or not raw_name.strip():
                return None
            name = raw_name.strip()
            if name not in group_ids_by_name:
                existing_group = await self.groups.get_by_name(name)
                group_ids_by_name[name] = existing_group.id if existing_group is not None else None
            return group_ids_by_name[name]

        for kind, model_raw, et in (
            ("clients", Client, "client"),
            ("accounts", Account, "account"),
            ("cards", Card, "card"),
        ):
            model = cast(Any, model_raw)
            for raw in _as_list(package.get(kind)):
                if not isinstance(raw, dict):
                    errors.append(f"{kind}: запись не является объектом")
                    continue
                raw = cast(dict[str, Any], raw)
                try:
                    # Canonical UUID string so dedup / parent lookups all key on
                    # the same form even if the file upper-cased the UUID.
                    eid = uuid.UUID(str(raw["id"]))
                    eid_str = str(eid)
                    if eid_str in seen_entity_ids[kind]:
                        errors.append(f"{kind} {eid_str}: дубликат в файле")
                        continue
                    seen_entity_ids[kind].add(eid_str)

                    existing_entity = await self.session.get(model, eid)
                    # For skip/fail on an existing row we don't write anything,
                    # so validation is deferred until we know we'll create/overwrite.
                    if existing_entity is not None and conflict(kind, raw):
                        continue

                    validated_attrs, err = await _validated_entity_attrs(et, kind, eid_str, raw)
                    if err or validated_attrs is None:
                        errors.append(err or f"{kind} {eid_str}: attributes отсутствуют")
                        continue
                    tags, tags_err = _validate_tags(raw.get("tags"))
                    if tags_err:
                        errors.append(f"{kind} {eid_str}: {tags_err}")
                        continue
                    description, desc_err = _validate_optional_str(raw.get("description"))
                    if desc_err:
                        errors.append(f"{kind} {eid_str}: description {desc_err}")
                        continue

                    # Parse the parent FK up front so a bad UUID can't leave a
                    # half-updated row dirty for the final commit. ``None`` means
                    # the field was absent (required on create, kept on overwrite).
                    new_client_id: uuid.UUID | None = None
                    new_account_id: uuid.UUID | None = None
                    if kind == "accounts" and raw.get("client_id"):
                        new_client_id = uuid.UUID(raw["client_id"])
                    if kind == "cards" and raw.get("account_id"):
                        new_account_id = uuid.UUID(raw["account_id"])

                    if existing_entity is None:
                        if kind == "accounts" and new_client_id is None:
                            errors.append(f"accounts {eid_str}: отсутствует client_id")
                            continue
                        if kind == "cards" and new_account_id is None:
                            errors.append(f"cards {eid_str}: отсутствует account_id")
                            continue

                    # Verify the referenced parent actually exists — already in
                    # the DB, or created earlier in this same import. A dangling
                    # FK would otherwise only surface as an FK error on the final
                    # commit and roll back the whole import instead of this row.
                    if new_client_id is not None:
                        client_id_str = str(new_client_id)
                        if (
                            client_id_str not in imported_new_ids["clients"]
                            and await self.clients.get(new_client_id) is None
                        ):
                            errors.append(f"accounts {eid_str}: клиент {client_id_str} не найден")
                            continue
                    if new_account_id is not None:
                        account_id_str = str(new_account_id)
                        if (
                            account_id_str not in imported_new_ids["accounts"]
                            and await self.accounts.get(new_account_id) is None
                        ):
                            errors.append(f"cards {eid_str}: счёт {account_id_str} не найден")
                            continue

                    # Group membership lives only on the client (accounts/cards
                    # inherit). Resolved by name against this instance's groups.
                    new_group_id = (
                        await resolve_group_id(raw.get("group_name")) if kind == "clients" else None
                    )

                    if existing_entity is None:
                        kwargs = {
                            "id": eid,
                            "description": description or "",
                            "tags": tags or [],
                            "attributes": validated_attrs,
                        }
                        if kind == "accounts":
                            kwargs["client_id"] = new_client_id
                        if kind == "cards":
                            kwargs["account_id"] = new_account_id
                        if kind == "clients":
                            kwargs["group_id"] = new_group_id
                        self.session.add(model(**kwargs))
                        created[kind] += 1
                        if kind in imported_new_ids:
                            imported_new_ids[kind].add(eid_str)
                    else:
                        # All values parsed above — apply atomically.
                        if description is not None:
                            existing_entity.description = description
                        if tags is not None:
                            existing_entity.tags = tags
                        existing_entity.attributes = validated_attrs
                        if new_client_id is not None:
                            existing_entity.client_id = new_client_id
                        if new_account_id is not None:
                            existing_entity.account_id = new_account_id
                        # Overwrite never *reduces* protection: only re-associate
                        # when the name resolves to a real group here. An absent /
                        # unknown group leaves the existing membership untouched,
                        # so restoring an old or group-less backup can't silently
                        # make a private client public.
                        if kind == "clients" and new_group_id is not None:
                            existing_entity.group_id = new_group_id
                        updated[kind] += 1
                except Exception as exc:
                    errors.append(f"{kind} {_safe_label(raw, 'id')}: {exc}")

        from app.utils import walker as _walker

        def _validate_template_body(label: str, fmt: str, body: str) -> str | None:
            """Return an error string if ``body`` doesn't parse as ``fmt``, else None."""

            try:
                if fmt == "json":
                    _walker.walk_json(body)
                else:
                    _walker.walk_xml(body)
            except Exception as exc:
                return f"template {label}: {fmt} не парсится: {exc}"
            return None

        # ---- collections (workspace import groups; must precede templates so
        # template.collection_id FKs resolve) ----
        seen_collection_ids: set[str] = set()
        for raw in _as_list(package.get("collections")):
            if not isinstance(raw, dict):
                errors.append("collections: запись не является объектом")
                continue
            raw = cast(dict[str, Any], raw)
            try:
                cid = uuid.UUID(str(raw["id"]))
                cid_str = str(cid)
                if cid_str in seen_collection_ids:
                    errors.append(f"collection {raw.get('name')}: дубликат id в файле")
                    continue
                seen_collection_ids.add(cid_str)

                existing_collection = await self.session.get(Collection, cid)
                if existing_collection is not None and conflict("collections", raw):
                    continue

                coll_name, coll_name_err = _validate_required_str(raw.get("name"), 255)
                if coll_name_err or coll_name is None:
                    errors.append(f"collection {_safe_label(raw, 'id')}: name {coll_name_err}")
                    continue
                coll_desc, coll_desc_err = _validate_optional_str(raw.get("description"))
                if coll_desc_err:
                    errors.append(f"collection {coll_name}: description {coll_desc_err}")
                    continue
                coll_source, coll_source_err = _validate_optional_str(raw.get("source"))
                if coll_source_err:
                    errors.append(f"collection {coll_name}: source {coll_source_err}")
                    continue
                coll_fmt, coll_fmt_err = _validate_optional_str(raw.get("source_format"))
                if coll_fmt_err:
                    errors.append(f"collection {coll_name}: source_format {coll_fmt_err}")
                    continue
                coll_vars = _as_list(raw.get("variables"))
                coll_folders = [
                    segments
                    for path in _as_list(raw.get("folders"))
                    if isinstance(path, list)
                    and (segments := [str(seg).strip() for seg in path if str(seg).strip()])
                ]

                if existing_collection is None:
                    self.session.add(Collection(
                        id=cid,
                        name=coll_name,
                        description=coll_desc or "",
                        source=coll_source or "postman",
                        source_format=coll_fmt or "",
                        variables=coll_vars,
                        folders=coll_folders,
                    ))
                    created["collections"] += 1
                else:
                    existing_collection.name = coll_name
                    existing_collection.description = (
                        coll_desc if coll_desc is not None else existing_collection.description
                    )
                    existing_collection.source = coll_source or existing_collection.source
                    existing_collection.source_format = (
                        coll_fmt if coll_fmt is not None else existing_collection.source_format
                    )
                    existing_collection.variables = coll_vars
                    existing_collection.folders = coll_folders
                    updated["collections"] += 1
            except Exception as exc:
                errors.append(f"collection {_safe_label(raw, 'name', 'id')}: {exc}")

        # Persist collections now so the template loop can resolve collection_id.
        if not await safe_flush("collections"):
            return _zeroed_summary()

        # ---- projects referenced by templates (resolved by name; created when
        # missing). Old packages without ``project_name`` land in «Без проекта»,
        # matching the migration semantics for pre-projects templates. The
        # permission is checked here — at the exact point a project would be
        # created — so only rows that genuinely need a new project are affected;
        # skipped conflicts, duplicates and invalid records never reach this. ----
        project_svc = ProjectService(self.session)
        project_ids_by_name: dict[str, uuid.UUID] = {}

        async def resolve_project_id(raw_name: Any, raw_color: Any) -> uuid.UUID:
            name = (
                raw_name.strip()
                if isinstance(raw_name, str) and raw_name.strip()
                else DEFAULT_PROJECT_NAME
            )[:255]
            if name not in project_ids_by_name:
                existing_project = await project_svc.repo.get_by_name(name)
                if existing_project is not None:
                    project_ids_by_name[name] = existing_project.id
                    return existing_project.id
                if not allow_project_creation:
                    raise ValidationFailed(
                        f"проект «{name}» не существует — создание проектов доступно "
                        "только в режиме редактирования настроек"
                    )
                color = (
                    raw_color
                    if isinstance(raw_color, str) and re.fullmatch(COLOR_PATTERN, raw_color)
                    else DEFAULT_PROJECT_COLOR
                )
                project = await project_svc.get_or_create_by_name(name, color=color)
                project_ids_by_name[name] = project.id
            return project_ids_by_name[name]

        # templates
        seen_template_ids: set[str] = set()
        for raw in _as_list(package.get("templates")):
            if not isinstance(raw, dict):
                errors.append("templates: запись не является объектом")
                continue
            raw = cast(dict[str, Any], raw)
            try:
                tid = uuid.UUID(str(raw["id"]))
                tid_str = str(tid)
                if tid_str in seen_template_ids:
                    errors.append(f"template {raw.get('name')}: дубликат id в файле")
                    continue
                seen_template_ids.add(tid_str)

                # For skip/fail on an existing template we don't write — defer
                # the (potentially expensive) content/placeholder validation.
                existing_template = await self.session.get(MessageTemplate, tid)
                if existing_template is not None and conflict("templates", raw):
                    continue

                fmt = raw.get("format", "json")
                if not isinstance(fmt, str) or fmt not in ("json", "xml"):
                    errors.append(f"template {raw.get('name')}: неподдерживаемый формат '{fmt}'")
                    continue

                # Parse every required field into a local before touching the
                # ORM object — a missing key must not leave the row half-updated.
                new_name, name_err = _validate_required_str(raw.get("name"), 255)
                if name_err or new_name is None:
                    errors.append(f"template {_safe_label(raw, 'id')}: name {name_err}")
                    continue
                new_content = raw.get("content")
                if not isinstance(new_content, str):
                    errors.append(f"template {new_name}: content должен быть строкой")
                    continue
                new_description, desc_err = _validate_optional_str(raw.get("description"))
                if desc_err:
                    errors.append(f"template {new_name}: description {desc_err}")
                    continue

                # A non-empty body should parse as the declared format. If it
                # does NOT, the template is imported as "unparsed" — kept verbatim
                # (mirrors the workspace import of GET/urlencoded/GraphQL requests
                # and the Postman parser's parsable=False bodies). LLM analysis and
                # fill are guarded elsewhere for such templates, so storing an
                # unparsable body is safe. Only a wrong *type* is rejected.
                content_parsable = bool(new_content) and (
                    _validate_template_body(raw.get("name", "?"), fmt, new_content) is None
                )

                # original_content: absent / empty / null → fall back to content.
                # A non-empty value is kept verbatim (not re-validated): if the
                # body is unparsable, so is its source.
                orig_raw = raw.get("original_content")
                if orig_raw is None or orig_raw == "":
                    new_original = new_content
                elif not isinstance(orig_raw, str):
                    errors.append(f"template {raw.get('name')}: original_content должен быть строкой")
                    continue
                else:
                    new_original = orig_raw

                # Validate placeholder structure — a malformed entry would later
                # crash regenerate/fill on this template.
                try:
                    placeholders = normalize_placeholders(raw.get("placeholders"))
                except ValidationFailed as vexc:
                    errors.append(f"template {raw.get('name')}: {vexc.message}")
                    continue

                # llm_meta semantics mirror TemplateUpdate on the CRUD path:
                #   absent / null → "do not change" (keep existing; create → {});
                #   explicit {}   → clear;
                #   {...}         → replace;
                #   non-object    → malformed file → row-level error.
                llm_meta_raw = raw.get("llm_meta")
                if llm_meta_raw is None:
                    new_llm_meta = existing_template.llm_meta if existing_template is not None else {}
                else:
                    parsed_llm_meta, meta_err = _validate_object_field(llm_meta_raw)
                    if meta_err or parsed_llm_meta is None:
                        errors.append(f"template {new_name}: llm_meta {meta_err}")
                        continue
                    new_llm_meta = parsed_llm_meta

                # Keep the unparsed marker and placeholders consistent: an
                # unparsable body cannot carry resolvable placeholders.
                if not content_parsable:
                    placeholders = []
                    new_llm_meta = {**new_llm_meta, "import_status": "unparsed"}

                # Workspace metadata (collection import). Optional — degrade to
                # defaults on absence/bad shape rather than failing the row.
                new_headers = [h for h in _as_list(raw.get("headers")) if isinstance(h, dict)]
                new_folder_path = [str(p) for p in _as_list(raw.get("folder_path"))]
                new_http_method, method_err = _validate_optional_str(raw.get("http_method"))
                if method_err:
                    errors.append(f"template {new_name}: http_method {method_err}")
                    continue
                new_url, url_err = _validate_optional_str(raw.get("url"))
                if url_err:
                    errors.append(f"template {new_name}: url {url_err}")
                    continue
                order_raw = raw.get("display_order")
                new_display_order = (
                    order_raw if isinstance(order_raw, int) and not isinstance(order_raw, bool) else 0
                )

                # Resolve collection_id only if that collection exists (imported
                # in the block above or pre-existing). An unknown id would fail
                # the FK; we make the template ungrouped instead.
                new_collection_id: uuid.UUID | None = None
                coll_ref = raw.get("collection_id")
                if coll_ref:
                    try:
                        candidate = uuid.UUID(str(coll_ref))
                    except (ValueError, AttributeError, TypeError):
                        candidate = None
                    if candidate is not None and await self.session.get(Collection, candidate) is not None:
                        new_collection_id = candidate

                new_project_id = await resolve_project_id(
                    raw.get("project_name"), raw.get("project_color")
                )

                if existing_template is None:
                    self.session.add(MessageTemplate(
                        id=tid,
                        project_id=new_project_id,
                        name=new_name,
                        description=new_description or "",
                        format=fmt,
                        content=new_content,
                        original_content=new_original,
                        llm_meta=new_llm_meta,
                        placeholders=placeholders,
                        collection_id=new_collection_id,
                        folder_path=new_folder_path,
                        headers=new_headers,
                        http_method=new_http_method or "",
                        url=new_url or "",
                        display_order=new_display_order,
                    ))
                    created["templates"] += 1
                else:
                    # all parsed — apply atomically
                    existing_template.project_id = new_project_id
                    existing_template.name = new_name
                    existing_template.description = (
                        new_description if new_description is not None else existing_template.description
                    )
                    existing_template.format = fmt
                    existing_template.content = new_content
                    existing_template.original_content = new_original
                    existing_template.llm_meta = new_llm_meta
                    existing_template.placeholders = placeholders
                    # Backups omit llm_debug by design; clear any debug left from
                    # the previous local template so it can't be misread as the
                    # prompts/response for the restored content.
                    existing_template.llm_debug = None
                    existing_template.collection_id = new_collection_id
                    existing_template.folder_path = new_folder_path
                    existing_template.headers = new_headers
                    existing_template.http_method = new_http_method or ""
                    existing_template.url = new_url or ""
                    existing_template.display_order = new_display_order
                    updated["templates"] += 1
            except Exception as exc:
                errors.append(f"template {_safe_label(raw, 'name', 'id')}: {exc}")

        # If fail-policy collected conflicts/errors, abort the whole transaction.
        if policy == "fail" and errors:
            await self.session.rollback()
            return _zeroed_summary()
        try:
            await self.session.commit()
        except (IntegrityError, SQLAlchemyError) as exc:
            await self.session.rollback()
            errors.append(f"commit failed: {exc.orig if hasattr(exc, 'orig') else exc}")
            # The transaction was rolled back, so nothing landed in DB.
            return _zeroed_summary()
        return ImportSummary(created=created, updated=updated, skipped=skipped, errors=errors)

    @staticmethod
    def _dump_attr(a: AttributeDefinition) -> dict[str, Any]:
        return {
            "id": str(a.id),
            "entity_type": a.entity_type,
            "name": a.name,
            "label": a.label,
            "data_type": a.data_type,
            "is_required": a.is_required,
            "display_order": a.display_order,
            "description": a.description,
            "options": a.options,
        }

    @staticmethod
    def _dump_client(c: Client) -> dict[str, Any]:
        # Access group is carried by name/color only — never the password hash.
        # On import it re-associates the client with a same-named group on the
        # destination, or leaves it public when none exists.
        group = getattr(c, "group", None)
        return {
            "id": str(c.id),
            "description": c.description,
            "tags": list(c.tags or []),
            "attributes": c.attributes,
            "group_name": getattr(group, "name", None),
            "group_color": getattr(group, "color", None),
        }

    @staticmethod
    def _dump_account(a: Account) -> dict[str, Any]:
        return {
            "id": str(a.id),
            "client_id": str(a.client_id),
            "description": a.description,
            "tags": list(a.tags or []),
            "attributes": a.attributes,
        }

    @staticmethod
    def _dump_card(c: Card) -> dict[str, Any]:
        return {
            "id": str(c.id),
            "account_id": str(c.account_id),
            "description": c.description,
            "tags": list(c.tags or []),
            "attributes": c.attributes,
        }

    @staticmethod
    def _dump_collection(c: Collection) -> dict[str, Any]:
        return {
            "id": str(c.id),
            "name": c.name,
            "description": c.description,
            "source": c.source,
            "source_format": c.source_format,
            "variables": c.variables,
            # Explicit folder structure (incl. empty folders); without this a
            # backup/restore would drop any folder that has no requests in it.
            "folders": [list(path) for path in (c.folders or [])],
        }

    @staticmethod
    def _dump_template(t: MessageTemplate) -> dict[str, Any]:
        return {
            "id": str(t.id),
            "name": t.name,
            "description": t.description,
            "format": t.format,
            "content": t.content,
            "original_content": t.original_content,
            "llm_meta": t.llm_meta,
            "placeholders": t.placeholders,
            # Project by name (+color) — restored via get_or_create on import so
            # the package is portable across instances with different project ids.
            "project_name": getattr(getattr(t, "project", None), "name", None),
            "project_color": getattr(getattr(t, "project", None), "color", None),
            # Workspace metadata (collection import) — kept so a backup/restore
            # preserves the collection tree, headers, method and URL.
            "collection_id": str(t.collection_id) if t.collection_id else None,
            "folder_path": list(t.folder_path or []),
            "headers": list(t.headers or []),
            "http_method": t.http_method,
            "url": t.url,
            "display_order": t.display_order,
        }
