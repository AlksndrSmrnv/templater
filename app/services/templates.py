from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageTemplate
from app.repositories.template import TemplateRepository
from app.schemas.template import TemplateCreate, TemplateUpdate
from app.services.attribute_schema import AttributeSchemaService
from app.utils import walker
from app.utils.errors import NotFoundError, ValidationFailed

PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


class TemplateService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = TemplateRepository(session)
        self.schema = AttributeSchemaService(session)

    async def list(self) -> list[MessageTemplate]:
        return await self.repo.list()

    async def get(self, template_id: uuid.UUID) -> MessageTemplate:
        t = await self.repo.get(template_id)
        if t is None:
            raise NotFoundError("Шаблон не найден")
        return t

    async def create(self, data: TemplateCreate) -> MessageTemplate:
        fmt = data.format
        if fmt not in ("json", "xml"):
            raise ValidationFailed("Неподдерживаемый формат шаблона")
        if not data.content.strip():
            raise ValidationFailed("Пустой шаблон")
        # validate format
        try:
            self._extract_leaves(fmt, data.content)
        except Exception as exc:
            raise ValidationFailed(f"Шаблон не парсится как {fmt}: {exc}")
        template = MessageTemplate(
            name=data.name,
            description=data.description,
            format=fmt,
            content=data.content,
            original_content=data.content,
            llm_meta={},
            placeholders=[],
        )
        await self.repo.add(template)
        await self.session.commit()
        await self.session.refresh(template)
        return template

    async def update(self, template_id: uuid.UUID, data: TemplateUpdate) -> MessageTemplate:
        template = await self.get(template_id)
        content_replaced = False
        if data.name is not None:
            template.name = data.name
        if data.description is not None:
            template.description = data.description
        if data.content is not None:
            # Verify the new body still parses as the template's declared format
            # so we don't end up with a broken envelope that later falls back to
            # raw-text substitution and bypasses JSON/XML escaping.
            try:
                self._extract_leaves(template.format, data.content)
            except Exception as exc:
                raise ValidationFailed(f"content не парсится как {template.format}: {exc}") from exc
            # Replacing the body is effectively a re-upload: sync the source of
            # truth (original_content) and drop existing placeholders, since
            # their locations are no longer guaranteed to exist in the new
            # document. Caller is expected to re-run analyze afterwards.
            template.content = data.content
            template.original_content = data.content
            template.placeholders = []
            content_replaced = True
        if data.llm_meta is not None:
            template.llm_meta = data.llm_meta
        # Placeholders coming alongside a content replacement are ignored — they
        # belonged to the old body. Anything else (e.g. an editor save without
        # touching content) is honored.
        if data.placeholders is not None and not content_replaced:
            template.placeholders = data.placeholders
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(template)
        return template

    async def delete(self, template_id: uuid.UUID) -> None:
        template = await self.get(template_id)
        await self.repo.delete(template)
        await self.session.commit()

    @staticmethod
    def _extract_leaves(fmt: str, content: str) -> list[walker.Leaf]:
        if fmt == "json":
            return walker.walk_json(content)
        if fmt == "xml":
            return walker.walk_xml(content)
        return []

    async def build_field_catalog(self) -> list[dict[str, str]]:
        """Return a flat list of placeholder paths available for substitution.

        Each entry: {"path": "sender.fullName", "label": "Sender — ФИО"}
        Generated from active attribute_definitions for client/account/card.
        """

        result: list[dict[str, str]] = []
        for role in ("sender", "receiver"):
            for entity, prefix in (("client", role), ("account", f"{role}.account"), ("card", f"{role}.card")):
                defs = await self.schema.list_schema(entity, include_deprecated=False)
                for d in defs:
                    result.append(
                        {
                            "path": f"{prefix}.{d.name}",
                            "label": f"{role}/{entity} — {d.label}",
                            "data_type": d.data_type,
                        }
                    )
        return result

    async def analyze(
        self,
        template: MessageTemplate,
        *,
        llm_service: Any | None = None,
    ) -> MessageTemplate:
        """Analyse the (original) template content and produce placeholders + llm_meta.

        Uses ``llm_service`` when given; otherwise falls back to a heuristic match
        based on attribute names (so the feature degrades gracefully without LLM).
        """

        source = template.original_content or template.content
        leaves = self._extract_leaves(template.format, source)
        catalog = await self.build_field_catalog()

        if llm_service is not None:
            result = await llm_service.analyze_template(
                content=source,
                fmt=template.format,
                leaves=[{"location": leaf.location, "value": leaf.value} for leaf in leaves],
                catalog=catalog,
            )
            mappings = {item["location"]: item for item in result.get("placeholders", [])}
            llm_meta = result.get("meta") or {}
        else:
            mappings = self._heuristic_mappings(leaves, catalog)
            llm_meta = {"summary": "Анализ выполнен без LLM (эвристика по именам полей)."}

        placeholders: list[dict[str, Any]] = []
        replacements: dict[str, str] = {}
        for leaf in leaves:
            m = mappings.get(leaf.location, {})
            suggestion = m.get("suggestion")
            mode = "mapped" if suggestion else "literal"
            current = f"{{{{{suggestion}}}}}" if suggestion else leaf.value
            placeholders.append(
                {
                    "location": leaf.location,
                    "original": leaf.value,
                    "mode": mode,
                    "value": current,
                    "suggestion": suggestion,
                }
            )
            if suggestion:
                replacements[leaf.location] = current

        new_content = source
        if replacements:
            new_content = (
                walker.replace_json(source, replacements)
                if template.format == "json"
                else walker.replace_xml(source, replacements)
            )

        template.content = new_content
        template.placeholders = placeholders
        template.llm_meta = llm_meta
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(template)
        return template

    @staticmethod
    def _heuristic_mappings(leaves: list[walker.Leaf], catalog: list[dict[str, str]]) -> dict[str, dict[str, str]]:
        """Match leaf paths to catalog entries by trailing-name similarity."""

        out: dict[str, dict[str, str]] = {}
        catalog_by_tail = {entry["path"].split(".")[-1].lower(): entry for entry in catalog}
        for leaf in leaves:
            # tail token of the JSON pointer / XML path
            tail = leaf.location.rstrip("/").split("/")[-1]
            tail = tail.replace("#text", "").lstrip("@")
            if "[" in tail:
                tail = tail.split("[", 1)[0]
            key = tail.lower()
            match = catalog_by_tail.get(key)
            if match:
                out[leaf.location] = {"suggestion": match["path"]}
        return out

    @staticmethod
    def regenerate_content(template: MessageTemplate) -> str:
        """Rebuild ``content`` from ``original_content`` + current ``placeholders``."""

        replacements: dict[str, str] = {}
        for ph in template.placeholders or []:
            if ph.get("mode") == "mapped":
                replacements[ph["location"]] = ph["value"]
            elif ph.get("mode") == "literal":
                # keep original; only override if value differs
                replacements[ph["location"]] = ph["value"]
        if not replacements:
            return template.original_content or template.content
        source = template.original_content or template.content
        return (
            walker.replace_json(source, replacements)
            if template.format == "json"
            else walker.replace_xml(source, replacements)
        )

    async def update_placeholders(
        self, template_id: uuid.UUID, placeholders: list[dict[str, Any]]
    ) -> MessageTemplate:
        template = await self.get(template_id)
        template.placeholders = placeholders
        template.content = self.regenerate_content(template)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(template)
        return template
