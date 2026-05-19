"""Selective export with dependency closure, and import with conflict policy."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
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
                elif policy == "overwrite":
                    existing.label = raw.get("label", existing.label)
                    existing.is_required = bool(raw.get("is_required", existing.is_required))
                    existing.is_deprecated = bool(raw.get("is_deprecated", existing.is_deprecated))
                    existing.display_order = int(raw.get("display_order", existing.display_order))
                    existing.description = raw.get("description", existing.description)
                    existing.options = raw.get("options", existing.options)
                    updated["attribute_schema"] += 1
                else:
                    skipped["attribute_schema"] += 1
            except Exception as exc:
                errors.append(f"attribute_schema {raw.get('name')}: {exc}")

        # references (by id)
        for ref_type, items in (package.get("references") or {}).items():
            for raw in items:
                try:
                    rid = uuid.UUID(raw["id"])
                    existing = await self.refs.get(rid)
                    if existing is None:
                        self.session.add(ReferenceValue(
                            id=rid,
                            entity_type=ref_type,
                            code=raw["code"],
                            name=raw["name"],
                            description=raw.get("description", ""),
                            attributes=raw.get("attributes", {}),
                        ))
                        created["references"] += 1
                    elif policy == "overwrite":
                        existing.code = raw["code"]
                        existing.name = raw["name"]
                        existing.description = raw.get("description", existing.description)
                        existing.attributes = raw.get("attributes", existing.attributes)
                        updated["references"] += 1
                    else:
                        skipped["references"] += 1
                except Exception as exc:
                    errors.append(f"reference {ref_type}/{raw.get('code')}: {exc}")

        # clients, accounts, cards (in dependency order)
        for kind, model in (("clients", Client), ("accounts", Account), ("cards", Card)):
            for raw in package.get(kind) or []:
                try:
                    eid = uuid.UUID(raw["id"])
                    existing = await self.session.get(model, eid)
                    if existing is None:
                        kwargs = {
                            "id": eid,
                            "description": raw.get("description", ""),
                            "tags": list(raw.get("tags", [])),
                            "attributes": raw.get("attributes", {}),
                        }
                        if kind == "accounts":
                            kwargs["client_id"] = uuid.UUID(raw["client_id"])
                        if kind == "cards":
                            kwargs["account_id"] = uuid.UUID(raw["account_id"])
                        self.session.add(model(**kwargs))
                        created[kind] += 1
                    elif policy == "overwrite":
                        existing.description = raw.get("description", existing.description)
                        existing.tags = list(raw.get("tags", existing.tags))
                        existing.attributes = raw.get("attributes", existing.attributes)
                        if kind == "accounts" and raw.get("client_id"):
                            existing.client_id = uuid.UUID(raw["client_id"])
                        if kind == "cards" and raw.get("account_id"):
                            existing.account_id = uuid.UUID(raw["account_id"])
                        updated[kind] += 1
                    else:
                        skipped[kind] += 1
                except Exception as exc:
                    errors.append(f"{kind} {raw.get('id')}: {exc}")

        # templates
        for raw in package.get("templates") or []:
            try:
                tid = uuid.UUID(raw["id"])
                existing = await self.session.get(MessageTemplate, tid)
                if existing is None:
                    self.session.add(MessageTemplate(
                        id=tid,
                        name=raw["name"],
                        description=raw.get("description", ""),
                        format=raw.get("format", "json"),
                        content=raw["content"],
                        original_content=raw.get("original_content", raw["content"]),
                        llm_meta=raw.get("llm_meta", {}),
                        placeholders=raw.get("placeholders", []),
                    ))
                    created["templates"] += 1
                elif policy == "overwrite":
                    existing.name = raw["name"]
                    existing.description = raw.get("description", existing.description)
                    existing.format = raw.get("format", existing.format)
                    existing.content = raw["content"]
                    existing.original_content = raw.get("original_content", existing.original_content)
                    existing.llm_meta = raw.get("llm_meta", existing.llm_meta)
                    existing.placeholders = raw.get("placeholders", existing.placeholders)
                    updated["templates"] += 1
                else:
                    skipped["templates"] += 1
            except Exception as exc:
                errors.append(f"template {raw.get('name')}: {exc}")

        if policy == "fail" and errors:
            await self.session.rollback()
            return ImportSummary(created=created, updated=updated, skipped=skipped, errors=errors)
        await self.session.commit()
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
