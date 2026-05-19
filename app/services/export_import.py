"""Selective export with dependency closure, and import with conflict policy."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
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
from app.schemas.exchange import ExportPackage, ExportRequest, ImportSummary


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
        created = {"attribute_schema": 0, "references": 0, "clients": 0, "accounts": 0, "cards": 0, "templates": 0}
        updated = {k: 0 for k in created}
        skipped = {k: 0 for k in created}
        errors: list[str] = []

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

        # attribute_schema (merge by entity_type + name)
        for raw in package.get("attribute_schema") or []:
            try:
                existing = await self.attrs.get_by_name(raw["entity_type"], raw["name"])
                if existing is None:
                    self.session.add(AttributeDefinition(
                        entity_type=raw["entity_type"],
                        name=raw["name"],
                        label=raw.get("label", raw["name"]),
                        data_type=raw.get("data_type", "string"),
                        is_required=bool(raw.get("is_required", False)),
                        is_deprecated=bool(raw.get("is_deprecated", False)),
                        display_order=int(raw.get("display_order", 0)),
                        description=raw.get("description", ""),
                        options=raw.get("options", {}),
                    ))
                    created["attribute_schema"] += 1
                elif not conflict("attribute_schema", raw):
                    existing.label = raw.get("label", existing.label)
                    existing.is_required = bool(raw.get("is_required", existing.is_required))
                    existing.is_deprecated = bool(raw.get("is_deprecated", existing.is_deprecated))
                    existing.display_order = int(raw.get("display_order", existing.display_order))
                    existing.description = raw.get("description", existing.description)
                    existing.options = raw.get("options", existing.options)
                    updated["attribute_schema"] += 1
            except Exception as exc:
                errors.append(f"attribute_schema {raw.get('name')}: {exc}")

        # Newly-added attribute_definitions are sitting in the session but not
        # yet visible to a fresh SELECT (autoflush=False). Flush so that the
        # ref-attributes coming from this same package are included in the
        # remap map below — otherwise ref-id rewrites would miss them.
        await self.session.flush()

        # Map of attribute_definitions used to detect ref-attributes that may
        # need remapping after we discover a (ref_type, code) collision.
        ref_attrs_by_owner: dict[str, list[tuple[str, str]]] = {}
        for d in await self.attrs.list_all():
            if d.data_type == "ref" and (d.options or {}).get("ref_entity"):
                ref_attrs_by_owner.setdefault(d.entity_type, []).append((d.name, d.options["ref_entity"]))

        # imported UUID → local UUID for any reference value that already exists
        # under a different id but the same (entity_type, code). Used to rewrite
        # ref-typed attributes in the imported clients/accounts/cards.
        ref_id_remap: dict[str, str] = {}

        # references (by id, then by (ref_type, code))
        for ref_type, items in (package.get("references") or {}).items():
            for raw in items:
                try:
                    rid = uuid.UUID(raw["id"])
                    existing = await self.refs.get(rid)
                    code_clash = None if existing is not None else await self.refs.get_by_code(ref_type, raw["code"])

                    if existing is None and code_clash is None:
                        self.session.add(ReferenceValue(
                            id=rid,
                            entity_type=ref_type,
                            code=raw["code"],
                            name=raw["name"],
                            description=raw.get("description", ""),
                            attributes=raw.get("attributes", {}),
                        ))
                        created["references"] += 1
                    elif existing is None and code_clash is not None:
                        # Same code under a different local id — remap imported uuid → local uuid.
                        ref_id_remap[str(rid)] = str(code_clash.id)
                        if not conflict("references", raw):
                            code_clash.name = raw["name"]
                            code_clash.description = raw.get("description", code_clash.description)
                            code_clash.attributes = raw.get("attributes", code_clash.attributes)
                            updated["references"] += 1
                    elif not conflict("references", raw):
                        # Existing by id. If the new code would collide with *another* row,
                        # surface it as an error rather than letting UNIQUE blow up on commit.
                        if existing.code != raw["code"]:
                            other = await self.refs.get_by_code(ref_type, raw["code"])
                            if other is not None and other.id != existing.id:
                                errors.append(
                                    f"reference {ref_type}/{raw['code']}: код уже занят другой записью"
                                )
                                continue
                            existing.code = raw["code"]
                        existing.name = raw["name"]
                        existing.description = raw.get("description", existing.description)
                        existing.attributes = raw.get("attributes", existing.attributes)
                        updated["references"] += 1
                except Exception as exc:
                    errors.append(f"reference {ref_type}/{raw.get('code')}: {exc}")

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
        for kind, model, et in (
            ("clients", Client, "client"),
            ("accounts", Account, "account"),
            ("cards", Card, "card"),
        ):
            for raw in package.get(kind) or []:
                try:
                    eid = uuid.UUID(raw["id"])
                    existing = await self.session.get(model, eid)
                    remapped_attrs = _remap_ref_attrs(et, raw.get("attributes") or {})
                    if existing is None:
                        kwargs = {
                            "id": eid,
                            "description": raw.get("description", ""),
                            "tags": list(raw.get("tags", [])),
                            "attributes": remapped_attrs,
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
                        existing.attributes = remapped_attrs or existing.attributes
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
        for raw in package.get("templates") or []:
            try:
                tid = uuid.UUID(raw["id"])
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
                        llm_meta=raw.get("llm_meta", {}),
                        placeholders=raw.get("placeholders", []),
                    ))
                    created["templates"] += 1
                elif not conflict("templates", raw):
                    existing.name = raw["name"]
                    existing.description = raw.get("description", existing.description)
                    existing.format = fmt
                    existing.content = raw["content"]
                    existing.original_content = raw.get("original_content", existing.original_content)
                    existing.llm_meta = raw.get("llm_meta", existing.llm_meta)
                    existing.placeholders = raw.get("placeholders", existing.placeholders)
                    updated["templates"] += 1
            except Exception as exc:
                errors.append(f"template {raw.get('name')}: {exc}")

        # If fail-policy collected conflicts/errors, abort the whole transaction.
        if policy == "fail" and errors:
            await self.session.rollback()
            # Nothing was actually written — collapse counters so the summary doesn't lie.
            return ImportSummary(
                created={k: 0 for k in created},
                updated={k: 0 for k in updated},
                skipped=skipped,
                errors=errors,
            )
        try:
            await self.session.commit()
        except (IntegrityError, SQLAlchemyError) as exc:
            await self.session.rollback()
            errors.append(f"commit failed: {exc.orig if hasattr(exc, 'orig') else exc}")
            # The transaction was rolled back, so nothing landed in DB.
            return ImportSummary(
                created={k: 0 for k in created},
                updated={k: 0 for k in updated},
                skipped={k: 0 for k in skipped},
                errors=errors,
            )
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
