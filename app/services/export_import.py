"""Selective export with dependency closure, and import with conflict policy."""

from __future__ import annotations

import json
import uuid
from typing import Any

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
from app.utils.errors import ValidationFailed


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce an external value to a dict.

    Tolerates the legacy bug where ``options`` was stored as a JSON string.
    Anything that isn't a dict (or a JSON string encoding one) becomes ``{}``.
    """

    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


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
            target = (d.options or {}).get("ref_entity")
            if target:
                ref_attrs_by_owner.setdefault(d.entity_type, []).append((d.name, target))

        ref_ids: dict[str, set[str]] = {t: set() for t in REFERENCE_TYPES}
        for entity_type, items in (("client", client_objs), ("account", account_objs), ("card", card_objs)):
            for o in items:
                for attr_name, target_type in ref_attrs_by_owner.get(entity_type, []):
                    val = (o.attributes or {}).get(attr_name)
                    if val:
                        ref_ids[target_type].add(str(val))

        references: dict[str, list[dict[str, Any]]] = {}
        for t, ids in ref_ids.items():
            if not ids:
                continue
            stmt = select(ReferenceValue).where(ReferenceValue.id.in_([uuid.UUID(i) for i in ids]))
            rows = list((await self.session.execute(stmt)).scalars().all())
            references[t] = [self._dump_reference(r) for r in rows]

        templates = await self.templates.get_many(req.templates)

        return ExportPackage(
            version=1,
            attribute_schema=[self._dump_attr(a) for a in all_defs],
            references=references,
            clients=[self._dump_client(c) for c in client_objs],
            accounts=[self._dump_account(a) for a in account_objs],
            cards=[self._dump_card(c) for c in card_objs],
            templates=[self._dump_template(t) for t in templates],
        )

    async def import_package(
        self,
        package: dict[str, Any],
        *,
        policy: str = "skip",
    ) -> ImportSummary:
        if policy not in ("skip", "overwrite", "fail"):
            policy = "skip"
        counter_keys = ("attribute_schema", "references", "clients", "accounts", "cards", "templates")
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
        for raw in package.get("attribute_schema") or []:
            try:
                entity_type = raw.get("entity_type")
                name = raw.get("name")
                if entity_type not in ALL_ATTR_ENTITY_TYPES:
                    errors.append(f"attribute_schema {name}: неизвестный entity_type '{entity_type}'")
                    continue
                if not name or not isinstance(name, str):
                    errors.append("attribute_schema: пустое или некорректное имя атрибута")
                    continue
                data_type = raw.get("data_type", "string")
                if data_type not in ALLOWED_TYPES:
                    errors.append(f"attribute_schema {entity_type}/{name}: неизвестный тип '{data_type}'")
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
                key = (entity_type, name)
                if key in seen_attr_keys:
                    errors.append(f"attribute_schema {entity_type}/{name}: дубликат в файле")
                    continue
                seen_attr_keys.add(key)

                existing = await self.attrs.get_by_name(entity_type, name)
                if existing is None:
                    self.session.add(AttributeDefinition(
                        entity_type=entity_type,
                        name=name,
                        label=raw.get("label") or name,
                        data_type=data_type,
                        is_required=bool(raw.get("is_required", False)),
                        is_deprecated=bool(raw.get("is_deprecated", False)),
                        display_order=int(raw.get("display_order", 0) or 0),
                        description=raw.get("description") or "",
                        options=options,
                    ))
                    created["attribute_schema"] += 1
                elif not conflict("attribute_schema", raw):
                    existing.label = raw.get("label") or existing.label
                    existing.is_required = bool(raw.get("is_required", existing.is_required))
                    existing.is_deprecated = bool(raw.get("is_deprecated", existing.is_deprecated))
                    existing.display_order = int(raw.get("display_order", existing.display_order) or 0)
                    existing.description = raw.get("description", existing.description) or ""
                    existing.options = options
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

        # references (by id, then by (ref_type, code))
        seen_ref_ids: set[str] = set()
        seen_ref_codes: set[tuple[str, str]] = set()
        for ref_type, items in (package.get("references") or {}).items():
            if ref_type not in REFERENCE_TYPES:
                errors.append(f"references: неизвестный тип справочника '{ref_type}'")
                continue
            for raw in items or []:
                try:
                    rid_str = str(raw["id"])
                    rid = uuid.UUID(rid_str)
                    code = raw["code"]
                    if rid_str in seen_ref_ids or (ref_type, code) in seen_ref_codes:
                        errors.append(f"reference {ref_type}/{code}: дубликат в файле")
                        continue
                    seen_ref_ids.add(rid_str)
                    seen_ref_codes.add((ref_type, code))

                    existing = await self.refs.get(rid)
                    code_clash = None if existing is not None else await self.refs.get_by_code(ref_type, code)

                    if existing is None and code_clash is None:
                        self.session.add(ReferenceValue(
                            id=rid,
                            entity_type=ref_type,
                            code=code,
                            name=raw["name"],
                            description=raw.get("description", ""),
                            attributes=_as_dict(raw.get("attributes")),
                        ))
                        created["references"] += 1
                    elif existing is None and code_clash is not None:
                        # Same code under a different local id — remap imported uuid → local uuid.
                        ref_id_remap[rid_str] = str(code_clash.id)
                        if not conflict("references", raw):
                            code_clash.name = raw["name"]
                            code_clash.description = raw.get("description", code_clash.description)
                            code_clash.attributes = _as_dict(raw.get("attributes")) or code_clash.attributes
                            updated["references"] += 1
                    elif not conflict("references", raw):
                        # Existing by id. If the new code would collide with *another* row,
                        # surface it as an error rather than letting UNIQUE blow up on commit.
                        if existing.code != code:
                            other = await self.refs.get_by_code(ref_type, code)
                            if other is not None and other.id != existing.id:
                                errors.append(
                                    f"reference {ref_type}/{code}: код уже занят другой записью"
                                )
                                continue
                            existing.code = code
                        existing.name = raw["name"]
                        existing.description = raw.get("description", existing.description)
                        existing.attributes = _as_dict(raw.get("attributes")) or existing.attributes
                        updated["references"] += 1
                except Exception as exc:
                    errors.append(f"reference {ref_type}/{raw.get('code')}: {exc}")

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
                if isinstance(val, str) and val in ref_id_remap:
                    out[attr_name] = ref_id_remap[val]
            return out

        # clients, accounts, cards (in dependency order)
        seen_entity_ids: dict[str, set[str]] = {"clients": set(), "accounts": set(), "cards": set()}
        for kind, model, et in (
            ("clients", Client, "client"),
            ("accounts", Account, "account"),
            ("cards", Card, "card"),
        ):
            for raw in package.get(kind) or []:
                try:
                    eid_str = str(raw["id"])
                    eid = uuid.UUID(eid_str)
                    if eid_str in seen_entity_ids[kind]:
                        errors.append(f"{kind} {eid_str}: дубликат в файле")
                        continue
                    seen_entity_ids[kind].add(eid_str)

                    existing = await self.session.get(model, eid)
                    remapped_attrs = _remap_ref_attrs(et, _as_dict(raw.get("attributes")))
                    # Run imported attributes through the same validation the
                    # CRUD path uses — checks required fields, casts types and
                    # verifies ref-ids exist — so import can't write broken JSONB.
                    try:
                        validated_attrs = await schema_svc.validate_attributes(et, remapped_attrs)
                    except ValidationFailed as vexc:
                        detail = (
                            "; ".join(vexc.details)
                            if isinstance(vexc.details, list)
                            else vexc.message
                        )
                        errors.append(f"{kind} {eid_str}: атрибуты не прошли проверку: {detail}")
                        continue

                    if existing is None:
                        kwargs = {
                            "id": eid,
                            "description": raw.get("description", ""),
                            "tags": list(raw.get("tags", [])),
                            "attributes": validated_attrs,
                        }
                        if kind == "accounts":
                            kwargs["client_id"] = uuid.UUID(raw["client_id"])
                        if kind == "cards":
                            kwargs["account_id"] = uuid.UUID(raw["account_id"])
                        self.session.add(model(**kwargs))
                        created[kind] += 1
                    elif not conflict(kind, raw):
                        existing.description = raw.get("description", existing.description)
                        existing.tags = list(raw.get("tags", existing.tags))
                        existing.attributes = validated_attrs
                        if kind == "accounts" and raw.get("client_id"):
                            existing.client_id = uuid.UUID(raw["client_id"])
                        if kind == "cards" and raw.get("account_id"):
                            existing.account_id = uuid.UUID(raw["account_id"])
                        updated[kind] += 1
                except Exception as exc:
                    errors.append(f"{kind} {raw.get('id')}: {exc}")

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

        # templates
        seen_template_ids: set[str] = set()
        for raw in package.get("templates") or []:
            try:
                tid_str = str(raw["id"])
                tid = uuid.UUID(tid_str)
                if tid_str in seen_template_ids:
                    errors.append(f"template {raw.get('name')}: дубликат id в файле")
                    continue
                seen_template_ids.add(tid_str)

                fmt = raw.get("format", "json")
                if fmt not in ("json", "xml"):
                    errors.append(f"template {raw.get('name')}: неподдерживаемый формат '{fmt}'")
                    continue

                # Both content and original_content must parse. The latter is
                # what analyze/regenerate later use as source, so a broken
                # original_content would manifest as a 500 in unrelated flows.
                content_err = _validate_template_body(raw.get("name", "?"), fmt, raw["content"])
                if content_err:
                    errors.append(content_err.replace("template", "template content"))
                    continue
                orig = raw.get("original_content")
                if orig and orig != raw["content"]:
                    orig_err = _validate_template_body(raw.get("name", "?"), fmt, orig)
                    if orig_err:
                        errors.append(orig_err.replace("template", "template original_content"))
                        continue

                existing = await self.session.get(MessageTemplate, tid)
                if existing is None:
                    self.session.add(MessageTemplate(
                        id=tid,
                        name=raw["name"],
                        description=raw.get("description", ""),
                        format=fmt,
                        content=raw["content"],
                        original_content=raw.get("original_content", raw["content"]),
                        llm_meta=_as_dict(raw.get("llm_meta")),
                        placeholders=raw.get("placeholders") if isinstance(raw.get("placeholders"), list) else [],
                    ))
                    created["templates"] += 1
                elif not conflict("templates", raw):
                    existing.name = raw["name"]
                    existing.description = raw.get("description", existing.description)
                    existing.format = fmt
                    existing.content = raw["content"]
                    existing.original_content = raw.get("original_content", existing.original_content)
                    existing.llm_meta = _as_dict(raw.get("llm_meta")) or existing.llm_meta
                    if isinstance(raw.get("placeholders"), list):
                        existing.placeholders = raw["placeholders"]
                    updated["templates"] += 1
            except Exception as exc:
                errors.append(f"template {raw.get('name')}: {exc}")

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
            "is_deprecated": a.is_deprecated,
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
        }
