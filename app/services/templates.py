from __future__ import annotations

import logging
import re
import uuid
from collections import Counter, defaultdict
from typing import Any, Protocol

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
from app.utils.paths import path_segments

log = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")
ACCOUNT_OWNER_TOKEN_RE = re.compile(r"\{\{\s*accountOwner\.")
FIELD_CATALOG_ENTITIES = ("client", "account", "card")
ENTITY_SCOPES = frozenset(entity for entity in FIELD_CATALOG_ENTITIES if entity != "client")


class TemplateAccountOwnerSource(Protocol):
    @property
    def format(self) -> str: ...

    @property
    def content(self) -> str: ...

    @property
    def original_content(self) -> str: ...

    @property
    def llm_meta(self) -> dict[str, Any] | None: ...

    @property
    def placeholders(self) -> list[dict[str, Any]] | None: ...


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


def content_has_account_owner(fmt: str, content: str | None) -> bool:
    if not content:
        return False
    try:
        if fmt == "json":
            leaves = walker.walk_json(content)
        elif fmt == "xml":
            leaves = walker.walk_xml(content)
        else:
            return False
    except Exception:
        log.debug("Unable to inspect template content for accountOwner role", exc_info=True)
        return False
    return any(resolve_role_from_path(leaf.location) == "accountOwner" for leaf in leaves)


def template_has_account_owner(template: TemplateAccountOwnerSource) -> bool:
    if placeholders_have_account_owner(template.placeholders or []):
        return True
    meta = template.llm_meta or {}
    if meta.get("has_account_owner") is True:
        return True
    if content_has_account_owner(template.format, template.content):
        return True
    if template.original_content != template.content:
        return content_has_account_owner(template.format, template.original_content)
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
            for entity in FIELD_CATALOG_ENTITIES:
                prefix = role if entity == "client" else f"{role}.{entity}"
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
                leaves=[leaf.location for leaf in leaves],
                catalog=catalog,
            )
            llm_mappings = self._llm_mappings_by_leaf(leaves, result.get("placeholders", []))
            llm_meta = result.get("meta") or {}
            llm_debug = result.get("debug")
        else:
            llm_mappings = {}
            llm_meta = {"summary": "Анализ выполнен без LLM (эвристика по именам полей)."}
            llm_debug = None

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
                catalog=catalog,
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
            "llm_debug": llm_debug,
        }

    @staticmethod
    def _resolve_suggestion(
        *,
        leaf: walker.Leaf,
        llm_suggestion: str | None,
        heuristic_suggestion: str | None,
        catalog: list[dict[str, str]],
        catalog_by_lower: dict[str, str],
    ) -> str | None:
        path_role = resolve_role_from_path(leaf.location)
        sources = (llm_suggestion, heuristic_suggestion)
        if path_role is not None:
            leaf_scope = TemplateService._entity_scope_from_segments(
                TemplateService._path_segments(leaf.location),
                path_role,
            )
            for source in sources:
                if not source:
                    continue
                cleaned = TemplateService._clean_suggestion(source)
                source_segments = TemplateService._path_segments(cleaned)
                tail = TemplateService._last_path_segment(source_segments)
                if tail is None:
                    continue
                source_scope = TemplateService._entity_scope_from_segments(source_segments, path_role)
                canonical = TemplateService._find_catalog_match(
                    catalog,
                    role=path_role,
                    attr=tail,
                    entity_scope=source_scope or leaf_scope,
                )
                if canonical:
                    return canonical
            return None

        for suggestion in sources:
            if suggestion:
                canonical = catalog_by_lower.get(
                    TemplateService._clean_suggestion(suggestion).lower()
                )
                if canonical:
                    return canonical
        return None

    @staticmethod
    def _llm_mappings_by_leaf(
        leaves: list[walker.Leaf],
        raw_placeholders: Any,
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(raw_placeholders, list):
            return {}

        exact: dict[str, tuple[int, dict[str, Any]]] = {}
        by_key: dict[tuple[str, ...], list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        placeholder_entries: list[
            tuple[int, dict[str, Any], tuple[str, ...], str | None]
        ] = []
        for raw_index, item in enumerate(raw_placeholders):
            if not isinstance(item, dict):
                continue
            location = item.get("location")
            if not isinstance(location, str) or not location:
                continue
            exact[location] = (raw_index, item)
            key = TemplateService._path_key(location)
            if key:
                by_key[key].append((raw_index, item))
            placeholder_entries.append((raw_index, item, key, resolve_role_from_path(location)))

        leaf_keys = {leaf.location: TemplateService._path_key(leaf.location) for leaf in leaves}
        leaf_key_counts = Counter(key for key in leaf_keys.values() if key)
        leaf_tail_role_counts = Counter(
            (key[-1], resolve_role_from_path(leaf.location))
            for leaf in leaves
            if (key := leaf_keys[leaf.location])
        )
        out: dict[str, dict[str, Any]] = {}
        matched_indexes: set[int] = set()
        for leaf in leaves:
            if leaf.location in exact:
                matched_index, matched_item = exact[leaf.location]
                out[leaf.location] = matched_item
                matched_indexes.add(matched_index)
                continue
            key = leaf_keys[leaf.location]
            if leaf_key_counts[key] == 1 and len(by_key.get(key, [])) == 1:
                matched_index, matched_item = by_key[key][0]
                out[leaf.location] = matched_item
                matched_indexes.add(matched_index)
                continue
            if not key:
                continue
            leaf_role = resolve_role_from_path(leaf.location)
            if leaf_role is None:
                continue
            tail_role = (key[-1], leaf_role)
            if leaf_tail_role_counts[tail_role] != 1:
                continue
            suffix_matches = [
                (candidate_index, item)
                for candidate_index, item, candidate_key, candidate_role in placeholder_entries
                if candidate_index not in matched_indexes
                and candidate_key
                and candidate_key[-1] == key[-1]
                and candidate_role == leaf_role
            ]
            if len(suffix_matches) == 1:
                matched_index, matched_item = suffix_matches[0]
                out[leaf.location] = matched_item
                matched_indexes.add(matched_index)
        unmatched = [
            {"location": item.get("location"), "suggestion": item.get("suggestion")}
            for placeholder_index, item, _, _ in placeholder_entries
            if placeholder_index not in matched_indexes
        ]
        if unmatched:
            log.warning("LLM returned %d unmatched placeholders: %r", len(unmatched), unmatched)
        return out

    @staticmethod
    def _find_catalog_match(
        catalog: list[dict[str, str]],
        *,
        role: str,
        attr: str,
        entity_scope: str | None,
    ) -> str | None:
        attr_key = attr.lower()
        role_key = role.lower()
        scoped_candidates: list[str] = []
        role_attr_candidates: list[str] = []
        for entry in catalog:
            path = entry["path"]
            parts = path.split(".")
            if not parts or parts[0].lower() != role_key or parts[-1].lower() != attr_key:
                continue
            catalog_scope = (
                parts[1].lower()
                if len(parts) > 2 and parts[1].lower() in ENTITY_SCOPES
                else None
            )
            if entity_scope is not None:
                if catalog_scope == entity_scope:
                    scoped_candidates.append(path)
            elif catalog_scope is None:
                role_attr_candidates.append(path)

        if entity_scope is not None:
            return scoped_candidates[0] if len(scoped_candidates) == 1 else None
        return role_attr_candidates[0] if len(role_attr_candidates) == 1 else None

    @staticmethod
    def _path_key(path: str) -> tuple[str, ...]:
        return tuple(segment.lower() for segment in TemplateService._path_segments(path))

    @staticmethod
    def _path_segments(path: str) -> list[str]:
        return path_segments(TemplateService._clean_suggestion(path))

    @staticmethod
    def _clean_suggestion(suggestion: str) -> str:
        value = suggestion.strip()
        match = PLACEHOLDER_RE.fullmatch(value)
        if match:
            return match.group(1).strip()
        return value

    @staticmethod
    def _last_path_segment(segments: list[str]) -> str | None:
        return segments[-1] if segments else None

    @staticmethod
    def _entity_scope_from_segments(segments: list[str], role: str | None) -> str | None:
        """Return entity scope from normalized path segments."""

        role_idx = TemplateService._last_role_segment_index(segments, role)
        search_segments = segments[role_idx + 1 :] if role_idx is not None else segments
        for segment in search_segments:
            if segment.lower() in ENTITY_SCOPES:
                return segment.lower()
        return None

    @staticmethod
    def _last_role_segment_index(segments: list[str], role: str | None) -> int | None:
        if role is None:
            return None
        for idx in range(len(segments) - 1, -1, -1):
            if resolve_role_from_path(segments[idx]) == role:
                return idx
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
