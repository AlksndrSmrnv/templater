from __future__ import annotations

import re
import uuid
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageTemplate
from app.repositories.template import TemplateRepository
from app.schemas.template import PlaceholderInfo, TemplateCreate, TemplateUpdate
from app.services.attribute_schema import AttributeSchemaService
from app.services.dynamic_fields import resolve_dynamic_token
from app.services.role_resolver import resolve_role_from_path
from app.utils import walker
from app.utils.errors import NotFoundError, ValidationFailed

PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")
ACCOUNT_OWNER_TOKEN_RE = re.compile(r"\{\{\s*accountOwner\.")


def placeholders_have_account_owner(placeholders: list[dict[str, Any]]) -> bool:
    for item in placeholders:
        if not isinstance(item, dict):
            continue
        suggestion = item.get("suggestion")
        if isinstance(suggestion, str) and suggestion.startswith("accountOwner."):
            return True
        value = item.get("value")
        if isinstance(value, str) and (
            value.startswith("accountOwner.") or ACCOUNT_OWNER_TOKEN_RE.search(value)
        ):
            return True
    return False


def normalize_placeholders(raw: Any) -> list[dict[str, Any]]:
    """Validate a raw placeholders payload and return clean dicts.

    Imported files and the editor API both send placeholders as free-form
    ``list[dict]``. Without structural checks a malformed entry (missing
    ``location``/``value``, bad ``mode``) would later crash regenerate /
    fill. Each item is validated through :class:`PlaceholderInfo`.
    """

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationFailed("placeholders должен быть списком")
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationFailed(f"placeholders[{idx}]: ожидается объект")
        try:
            validated = PlaceholderInfo.model_validate(item)
        except PydanticValidationError as exc:
            raise ValidationFailed(
                f"placeholders[{idx}]: некорректная структура: {exc.errors()}"
            ) from exc
        out.append(validated.model_dump())
    return out


class TemplateService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = TemplateRepository(session)
        self.schema = AttributeSchemaService(session)

    async def list_all(self) -> list[MessageTemplate]:
        return await self.repo.list_all()

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
        # touching content) is honored after structural validation.
        if data.placeholders is not None and not content_replaced:
            template.placeholders = normalize_placeholders(data.placeholders)
        await self.session.flush()
        return template

    async def delete(self, template_id: uuid.UUID) -> None:
        template = await self.get(template_id)
        await self.repo.delete(template)

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
        for role in ("sender", "receiver", "accountOwner"):
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
        result = await self.analyze_content(
            fmt=template.format,
            original_content=source,
            llm_service=llm_service,
        )
        template.content = result["content"]
        template.placeholders = result["placeholders"]
        template.llm_meta = result["llm_meta"]
        await self.session.flush()
        return template

    async def analyze_content(
        self,
        *,
        fmt: str,
        original_content: str,
        llm_service: Any | None = None,
    ) -> dict[str, Any]:
        """Analyze raw template content without creating or mutating a DB row."""

        leaves = self._extract_leaves(fmt, original_content)
        catalog = await self.build_field_catalog()
        heuristic_mappings = self._heuristic_mappings(leaves, catalog)
        catalog_by_lower = {entry["path"].lower(): entry["path"] for entry in catalog}

        if llm_service is not None:
            result = await llm_service.analyze_template(
                content=original_content,
                fmt=fmt,
                leaves=[{"location": leaf.location, "value": leaf.value} for leaf in leaves],
                catalog=catalog,
            )
            llm_mappings = {item["location"]: item for item in result.get("placeholders", [])}
            llm_meta = result.get("meta") or {}
        else:
            llm_mappings = {}
            llm_meta = {"summary": "Анализ выполнен без LLM (эвристика по именам полей)."}

        placeholders: list[dict[str, Any]] = []
        replacements: dict[str, str] = {}
        for leaf in leaves:
            dynamic_token = resolve_dynamic_token(leaf.location)
            if dynamic_token is not None:
                token_value = f"{{{{{dynamic_token}}}}}"
                placeholders.append(
                    {
                        "location": leaf.location,
                        "original": leaf.value,
                        "mode": "dynamic",
                        "value": token_value,
                        "suggestion": dynamic_token,
                    }
                )
                replacements[leaf.location] = token_value
                continue

            llm_suggestion = llm_mappings.get(leaf.location, {}).get("suggestion")
            heuristic_suggestion = heuristic_mappings.get(leaf.location, {}).get("suggestion")
            suggestion = self._resolve_suggestion(
                leaf=leaf,
                llm_suggestion=llm_suggestion if isinstance(llm_suggestion, str) else None,
                heuristic_suggestion=heuristic_suggestion
                if isinstance(heuristic_suggestion, str)
                else None,
                catalog_by_lower=catalog_by_lower,
            )
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

        new_content = original_content
        if replacements:
            new_content = (
                walker.replace_json(original_content, replacements)
                if fmt == "json"
                else walker.replace_xml(original_content, replacements)
            )

        return {
            "content": new_content,
            "placeholders": placeholders,
            "llm_meta": {
                **llm_meta,
                "has_account_owner": placeholders_have_account_owner(placeholders),
            },
        }

    @staticmethod
    def _resolve_suggestion(
        *,
        leaf: walker.Leaf,
        llm_suggestion: str | None,
        heuristic_suggestion: str | None,
        catalog_by_lower: dict[str, str],
    ) -> str | None:
        path_role = resolve_role_from_path(leaf.location)
        if path_role is not None:
            source = llm_suggestion or heuristic_suggestion
            if source is None or "." not in source:
                return None
            attr = source.split(".", 1)[1]
            return catalog_by_lower.get(f"{path_role}.{attr}".lower())

        for suggestion in (llm_suggestion, heuristic_suggestion):
            if suggestion:
                canonical = catalog_by_lower.get(suggestion.lower())
                if canonical:
                    return canonical
        return None

    @staticmethod
    def _heuristic_mappings(
        leaves: list[walker.Leaf],
        catalog: list[dict[str, str]],
    ) -> dict[str, dict[str, str]]:
        """Match leaf paths to catalog entries by trailing-name similarity."""

        out: dict[str, dict[str, str]] = {}
        catalog_by_tail: dict[str, list[dict[str, str]]] = {}
        for entry in catalog:
            catalog_by_tail.setdefault(entry["path"].split(".")[-1].lower(), []).append(entry)
        for leaf in leaves:
            # tail token of the JSON pointer / XML path
            tail = leaf.location.rstrip("/").split("/")[-1]
            tail = tail.replace("#text", "").lstrip("@")
            if "[" in tail:
                tail = tail.split("[", 1)[0]
            key = tail.lower()
            matches = catalog_by_tail.get(key, [])
            match = TemplateService._choose_catalog_match(leaf, matches)
            if match:
                out[leaf.location] = {"suggestion": match["path"]}
        return out

    @staticmethod
    def _choose_catalog_match(
        leaf: walker.Leaf,
        matches: list[dict[str, str]],
    ) -> dict[str, str] | None:
        if not matches:
            return None
        haystack = f"{leaf.location} {leaf.value}".lower()
        owner_markers = (
            "owner",
            "accountowner",
            "account_owner",
            "accountholder",
            "account_holder",
            "владелец",
            "держатель",
        )
        if any(marker in haystack for marker in owner_markers):
            for match in matches:
                if match["path"].startswith("accountOwner."):
                    return match
        non_owner = [match for match in matches if not match["path"].startswith("accountOwner.")]
        return non_owner[-1] if non_owner else matches[-1]

    @staticmethod
    def regenerate_content(template: MessageTemplate) -> str:
        """Rebuild ``content`` from ``original_content`` + current ``placeholders``.

        Defensive against malformed placeholder entries: anything missing
        ``location``/``value`` is skipped rather than raising. ``replace_json`` /
        ``replace_xml`` themselves tolerate paths that no longer resolve.
        """

        replacements: dict[str, str] = {}
        for ph in template.placeholders or []:
            if not isinstance(ph, dict):
                continue
            location = ph.get("location")
            value = ph.get("value")
            if not location or value is None:
                continue
            if ph.get("mode") in ("mapped", "literal", "dynamic"):
                replacements[location] = value
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
        template.placeholders = normalize_placeholders(placeholders)
        template.llm_meta = {
            **(template.llm_meta or {}),
            "has_account_owner": placeholders_have_account_owner(template.placeholders),
        }
        template.content = self.regenerate_content(template)
        await self.session.flush()
        return template
