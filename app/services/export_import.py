"""Selective export with dependency closure, and import with conflict policy."""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ALL_ATTR_ENTITY_TYPES,
    REFERENCE_TYPES,
    Account,
    AttributeDefinition,
    Card,
    Client,
    Collection,
    MessageTemplate,
    ReferenceValue,
)
from app.repositories.attribute import AttributeDefinitionRepository
from app.repositories.entity import (
    AccountRepository,
    CardRepository,
    ClientRepository,
)
from app.repositories.reference import ReferenceValueRepository
from app.repositories.template import TemplateRepository
from app.schemas.attribute import ALLOWED_TYPES
from app.schemas.exchange import ExportPackage, ExportRequest, ImportSummary
from app.services.attribute_schema import AttributeSchemaService
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


def _ref_write_fields(
    ref_type: str, code: str, raw: dict[str, Any], fallback_description: str
) -> tuple[str | None, str | None, str | None]:
    """Validate ``name`` (required) and ``description`` (optional) for a
    reference write, mirroring the CRUD Pydantic constraints.

    Returns ``(name, description, error)``. ``description`` falls back to
    ``fallback_description`` when the field is absent.
    """

    name, name_err = _validate_required_str(raw.get("name"), 255)
    if name_err:
        return None, None, f"reference {ref_type}/{code}: name {name_err}"
    desc, desc_err = _validate_optional_str(raw.get("description"))
    if desc_err:
        return None, None, f"reference {ref_type}/{code}: description {desc_err}"
    return name, (desc if desc is not None else fallback_description), None


class ExportImportService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.attrs = AttributeDefinitionRepository(session)
        self.refs = ReferenceValueRepository(session)
        self.clients = ClientRepository(session)
        self.accounts = AccountRepository(session)
        self.cards = CardRepository(session)
        self.templates = TemplateRepository(session)

    async def export(self, req: ExportRequest) -> ExportPackage:
        # gather: cards -> their accounts -> their clients
        card_objs = await self.cards.get_many(req.cards)
        account_ids = set(req.accounts) | {c.account_id for c in card_objs}
        account_objs = await self.accounts.get_many(list(account_ids))
        client_ids = set(req.clients) | {a.client_id for a in account_objs}
        client_objs = await self.clients.get_many(list(client_ids))

        # collect reference IDs used in attributes
        all_defs = await self.attrs.list_all()
        ref_attrs_by_owner: dict[str, list[tuple[str, str]]] = {}
        for d in all_defs:
            if d.data_type != "ref":
                continue
            target = _as_dict(d.options).get("ref_entity")
            if target in REFERENCE_TYPES:
                ref_attrs_by_owner.setdefault(d.entity_type, []).append((d.name, target))

        ref_ids: dict[str, set[uuid.UUID]] = {t: set() for t in REFERENCE_TYPES}
        for entity_type, items in (("client", client_objs), ("account", account_objs), ("card", card_objs)):
            for o in items:
                for attr_name, target_type in ref_attrs_by_owner.get(entity_type, []):
                    val = (o.attributes or {}).get(attr_name)
                    if not val:
                        continue
                    try:
                        # A legacy value of a since-deleted attribute may not be a
                        # UUID (validation preserves unknown keys unchecked).
                        # Skip it instead of letting uuid.UUID(...) 500 the export.
                        ref_ids[target_type].add(uuid.UUID(str(val)))
                    except (ValueError, AttributeError, TypeError):
                        continue

        references: dict[str, list[dict[str, Any]]] = {}
        for t, ids in ref_ids.items():
            if not ids:
                continue
            stmt = select(ReferenceValue).where(ReferenceValue.id.in_(list(ids)))
            rows = list((await self.session.execute(stmt)).scalars().all())
            references[t] = [self._dump_reference(r) for r in rows]

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
            references=references,
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
    ) -> ImportSummary:
        if policy not in ("skip", "overwrite", "fail"):
            policy = "skip"
        counter_keys = (
            "attribute_schema",
            "references",
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
        references_value = package.get("references")
        if references_value is not None and not isinstance(references_value, dict):
            errors.append("Секция 'references' должна быть объектом {тип: [...]}")
            shape_errors = True
        if shape_errors:
            return _zeroed_summary()

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
                if not isinstance(entity_type_raw, str) or entity_type_raw not in ALL_ATTR_ENTITY_TYPES:
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

                # data_type is immutable: turning an existing enum/ref attribute
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
                if data_type == "ref" and options.get("ref_entity") not in REFERENCE_TYPES:
                    errors.append(f"attribute_schema {entity_type}/{name}: ref_entity вне справочников")
                    continue
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

        # Map of attribute_definitions used to detect ref-attributes that may
        # need remapping after we discover a (ref_type, code) collision.
        ref_attrs_by_owner: dict[str, list[tuple[str, str]]] = {}
        for d in await self.attrs.list_all():
            if d.data_type == "ref" and _as_dict(d.options).get("ref_entity"):
                ref_attrs_by_owner.setdefault(d.entity_type, []).append(
                    (d.name, _as_dict(d.options)["ref_entity"])
                )

        # imported UUID → local UUID for any reference value that already exists
        # under a different id but the same (entity_type, code). Used to rewrite
        # ref-typed attributes in the imported clients/accounts/cards.
        ref_id_remap: dict[str, str] = {}

        async def _validated_ref_attrs(
            ref_type: str, label: str, raw: dict[str, Any]
        ) -> tuple[dict[str, Any] | None, str | None]:
            """Validate reference attributes like the CRUD path. Only called when
            we're actually about to write (create / overwrite)."""

            attrs_raw, attrs_err = _validate_attributes_field(raw.get("attributes"))
            if attrs_err:
                return None, f"reference {ref_type}/{label}: {attrs_err}"
            assert attrs_raw is not None
            try:
                attrs = await schema_svc.validate_attributes(ref_type, attrs_raw)
                return attrs, None
            except ValidationFailed as vexc:
                detail = "; ".join(vexc.details) if isinstance(vexc.details, list) else vexc.message
                return None, f"reference {ref_type}/{label}: атрибуты не прошли проверку: {detail}"

        # references (by id, then by (ref_type, code))
        seen_ref_ids: set[str] = set()
        seen_ref_codes: set[tuple[str, str]] = set()
        references_section = package.get("references")
        if references_section and not isinstance(references_section, dict):
            errors.append("references: ожидался объект вида {тип: [...]}")
            references_section = {}
        references_map = cast(dict[str, Any], references_section or {})
        # Prefetch existing references so the per-row lookups below are dict hits
        # instead of 2-3 queries each (autoflush is off and nothing is committed
        # mid-loop, so a snapshot taken now matches what live queries would return).
        present_ref_types = [t for t in references_map if t in REFERENCE_TYPES]
        existing_ref_by_id: dict[str, ReferenceValue] = {}
        existing_ref_by_code: dict[tuple[str, str], ReferenceValue] = {}
        if present_ref_types:
            # Codes only ever clash within a type → scan existing rows of the
            # present types to build the (type, code) index.
            for row in (
                (
                    await self.session.execute(
                        select(ReferenceValue).where(
                            ReferenceValue.entity_type.in_(present_ref_types)
                        )
                    )
                )
                .scalars()
                .all()
            ):
                existing_ref_by_code[(row.entity_type, row.code)] = row
            # The id (PK) guard is cross-type: a UUID may already belong to a row of
            # ANY type, so the id index must be built by id regardless of type —
            # otherwise the cross-type collision check below is bypassed and the
            # duplicate PK only surfaces as a whole-stage rollback on flush.
            file_ref_ids = {
                rid for items in references_map.values() for rid in _collect_uuids(items, "id")
            }
            if file_ref_ids:
                for row in (
                    (
                        await self.session.execute(
                            select(ReferenceValue).where(ReferenceValue.id.in_(file_ref_ids))
                        )
                    )
                    .scalars()
                    .all()
                ):
                    existing_ref_by_id[str(row.id)] = row
        for ref_type, items in references_map.items():
            if ref_type not in REFERENCE_TYPES:
                errors.append(f"references: неизвестный тип справочника '{ref_type}'")
                continue
            if not isinstance(items, list):
                # Nested value must itself be a list — otherwise _as_list would
                # silently drop it and the rows would vanish without an error.
                errors.append(f"references.{ref_type}: должно быть списком")
                continue
            for raw in items:
                if not isinstance(raw, dict):
                    errors.append(f"reference {ref_type}: запись не является объектом")
                    continue
                raw = cast(dict[str, Any], raw)
                try:
                    # Canonicalize: a file may carry the UUID upper-cased; use
                    # str(parsed) everywhere so dedup / remap / parent lookups
                    # all key on the same form.
                    rid = uuid.UUID(str(raw["id"]))
                    rid_str = str(rid)
                    # code is used for lookup + dedup, so validate it up front.
                    code, code_err = _validate_required_str(raw.get("code"), 128)
                    if code_err or code is None:
                        errors.append(f"reference {ref_type}: code {code_err}")
                        continue
                    if rid_str in seen_ref_ids or (ref_type, code) in seen_ref_codes:
                        errors.append(f"reference {ref_type}/{code}: дубликат в файле")
                        continue
                    seen_ref_ids.add(rid_str)
                    seen_ref_codes.add((ref_type, code))

                    existing_ref = existing_ref_by_id.get(rid_str)
                    if existing_ref is not None and existing_ref.entity_type != ref_type:
                        # The UUID already belongs to a different reference table.
                        # Overwriting would corrupt that row with this type's
                        # payload/schema — reject the cross-type collision.
                        errors.append(
                            f"reference {ref_type}/{code}: UUID {rid_str} уже занят записью "
                            f"типа '{existing_ref.entity_type}'"
                        )
                        continue
                    code_clash = (
                        None if existing_ref is not None else existing_ref_by_code.get((ref_type, code))
                    )

                    if existing_ref is None and code_clash is None:
                        # CREATE — validate the payload we're about to write.
                        ref_attrs, err = await _validated_ref_attrs(ref_type, code, raw)
                        if err or ref_attrs is None:
                            errors.append(err or f"reference {ref_type}/{code}: attributes отсутствуют")
                            continue
                        ref_name, ref_desc, fld_err = _ref_write_fields(ref_type, code, raw, "")
                        if fld_err or ref_name is None or ref_desc is None:
                            errors.append(fld_err or f"reference {ref_type}/{code}: некорректные поля")
                            continue
                        self.session.add(ReferenceValue(
                            id=rid,
                            entity_type=ref_type,
                            code=code,
                            name=ref_name,
                            description=ref_desc,
                            attributes=ref_attrs,
                        ))
                        created["references"] += 1
                    elif existing_ref is None and code_clash is not None:
                        # Same code under a different local id. Remap the imported
                        # UUID → local UUID UNCONDITIONALLY (even on skip), so the
                        # dependent clients/accounts/cards still resolve.
                        ref_id_remap[rid_str] = str(code_clash.id)
                        if conflict("references", raw):
                            continue  # skip/fail: don't touch the local row, don't validate
                        ref_attrs, err = await _validated_ref_attrs(ref_type, code, raw)
                        if err or ref_attrs is None:
                            errors.append(err or f"reference {ref_type}/{code}: attributes отсутствуют")
                            continue
                        # Parse every new value before touching the ORM object,
                        # so a missing field can't leave the row half-updated.
                        ref_name, ref_desc, fld_err = _ref_write_fields(
                            ref_type, code, raw, code_clash.description
                        )
                        if fld_err or ref_name is None or ref_desc is None:
                            errors.append(fld_err or f"reference {ref_type}/{code}: некорректные поля")
                            continue
                        code_clash.name = ref_name
                        code_clash.description = ref_desc
                        code_clash.attributes = ref_attrs
                        updated["references"] += 1
                    else:
                        # Existing by id.
                        if conflict("references", raw):
                            continue  # skip/fail: leave it, no validation needed
                        assert existing_ref is not None
                        # If the new code would collide with *another* row, surface
                        # it rather than letting UNIQUE blow up on commit.
                        if existing_ref.code != code:
                            other = existing_ref_by_code.get((ref_type, code))
                            if other is not None and other.id != existing_ref.id:
                                errors.append(
                                    f"reference {ref_type}/{code}: код уже занят другой записью"
                                )
                                continue
                        ref_attrs, err = await _validated_ref_attrs(ref_type, code, raw)
                        if err or ref_attrs is None:
                            errors.append(err or f"reference {ref_type}/{code}: attributes отсутствуют")
                            continue
                        # Parse every new value before touching the ORM object.
                        ref_name, ref_desc, fld_err = _ref_write_fields(
                            ref_type, code, raw, existing_ref.description
                        )
                        if fld_err or ref_name is None or ref_desc is None:
                            errors.append(fld_err or f"reference {ref_type}/{code}: некорректные поля")
                            continue
                        existing_ref.code = code
                        existing_ref.name = ref_name
                        existing_ref.description = ref_desc
                        existing_ref.attributes = ref_attrs
                        updated["references"] += 1
                except Exception as exc:
                    errors.append(f"reference {ref_type}/{_safe_label(raw, 'code', 'id')}: {exc}")

        # Flush references so validate_attributes() below can verify ref-typed
        # attributes against rows imported in this same transaction.
        if not await safe_flush("references"):
            return _zeroed_summary()

        def _remap_ref_attrs(entity_type: str, attrs: dict[str, Any]) -> dict[str, Any]:
            if not ref_id_remap or not attrs:
                return attrs
            out = dict(attrs)
            for attr_name, _ref_type in ref_attrs_by_owner.get(entity_type, []):
                val = out.get(attr_name)
                if not isinstance(val, str):
                    continue
                # ref_id_remap keys are canonical UUID strings — canonicalize the
                # imported attribute value before lookup so a differently-cased
                # UUID still matches.
                try:
                    canonical = str(uuid.UUID(val))
                except ValueError:
                    continue
                if canonical in ref_id_remap:
                    out[attr_name] = ref_id_remap[canonical]
            return out

        async def _validated_entity_attrs(
            et: str, kind: str, label: str, raw: dict[str, Any]
        ) -> tuple[dict[str, Any] | None, str | None]:
            """Remap ref-ids then validate like the CRUD path. Only called when
            we're actually about to write (create / overwrite)."""

            attrs_raw, attrs_err = _validate_attributes_field(raw.get("attributes"))
            if attrs_err:
                return None, f"{kind} {label}: {attrs_err}"
            assert attrs_raw is not None
            remapped = _remap_ref_attrs(et, attrs_raw)
            try:
                attrs = await schema_svc.validate_attributes(et, remapped)
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
                    # Canonical UUID string (see references loop note above).
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

                if existing_collection is None:
                    self.session.add(Collection(
                        id=cid,
                        name=coll_name,
                        description=coll_desc or "",
                        source=coll_source or "postman",
                        source_format=coll_fmt or "",
                        variables=coll_vars,
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
                    updated["collections"] += 1
            except Exception as exc:
                errors.append(f"collection {_safe_label(raw, 'name', 'id')}: {exc}")

        # Persist collections now so the template loop can resolve collection_id.
        if not await safe_flush("collections"):
            return _zeroed_summary()

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

                if existing_template is None:
                    self.session.add(MessageTemplate(
                        id=tid,
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
                    existing_template.name = new_name
                    existing_template.description = (
                        new_description if new_description is not None else existing_template.description
                    )
                    existing_template.format = fmt
                    existing_template.content = new_content
                    existing_template.original_content = new_original
                    existing_template.llm_meta = new_llm_meta
                    existing_template.placeholders = placeholders
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
    def _dump_reference(r: ReferenceValue) -> dict[str, Any]:
        return {
            "id": str(r.id),
            "code": r.code,
            "name": r.name,
            "description": r.description,
            "attributes": r.attributes,
        }

    @staticmethod
    def _dump_client(c: Client) -> dict[str, Any]:
        return {
            "id": str(c.id),
            "description": c.description,
            "tags": list(c.tags or []),
            "attributes": c.attributes,
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
            # Workspace metadata (collection import) — kept so a backup/restore
            # preserves the collection tree, headers, method and URL.
            "collection_id": str(t.collection_id) if t.collection_id else None,
            "folder_path": list(t.folder_path or []),
            "headers": list(t.headers or []),
            "http_method": t.http_method,
            "url": t.url,
            "display_order": t.display_order,
        }
