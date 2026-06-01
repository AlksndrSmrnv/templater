"""Import external request collections and run LLM analysis across them.

A collection is parsed (see :mod:`app.services.importers`) into a
:class:`~app.services.importers.base.ParsedCollection`; each request becomes a
``MessageTemplate`` linked to the new ``Collection`` row. Rows are created
directly through the repository (not :meth:`TemplateService.create`) so requests
with non-parsable bodies (GET, urlencoded, …) are still imported.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Collection, MessageTemplate
from app.llm.runner import llm_service
from app.repositories.collection import CollectionRepository
from app.repositories.template import TemplateRepository
from app.schemas.collection import ImportCollectionSummary, ProcessCollectionSummary
from app.services.importers import parse_postman_collection
from app.services.templates import TemplateService, apply_dynamic_headers
from app.utils.errors import NotFoundError, ValidationFailed

log = logging.getLogger(__name__)


def _haystack(template: MessageTemplate) -> str:
    meta = template.llm_meta or {}
    return " ".join(
        str(part)
        for part in (
            template.name,
            template.description,
            template.url,
            template.http_method,
            meta.get("summary", ""),
            meta.get("category", ""),
        )
    ).lower()


def _new_node() -> dict[str, Any]:
    return {"folders": {}, "templates": []}


def build_folder_tree(
    templates: list[MessageTemplate],
    extra_folders: list[list[str]] | None = None,
) -> dict[str, Any]:
    """Group templates into a nested ``{"folders": {...}, "templates": [...]}``.

    Folders come from each template's materialised ``folder_path``. Order within
    a level follows ``display_order`` then ``created_at`` (preserving import
    order). ``extra_folders`` (a collection's explicit folder paths) are seeded
    into the tree even when empty, so folders with no requests still appear.
    Consumed by ``partials/collections_tree.html`` via a recursive macro.
    """

    root = _new_node()
    ordered = sorted(templates, key=lambda t: (t.display_order, t.created_at))
    for template in ordered:
        node = root
        for folder in template.folder_path or []:
            node = node["folders"].setdefault(str(folder), _new_node())
        node["templates"].append(template)
    for path in extra_folders or []:
        node = root
        for folder in path:
            node = node["folders"].setdefault(str(folder), _new_node())
    return root


def _norm_path(path: list[Any] | None) -> list[str]:
    """Coerce a folder path into a list of non-empty string segments."""

    return [str(seg).strip() for seg in (path or []) if str(seg).strip()]


def _starts_with(path: list[str], prefix: list[str]) -> bool:
    return path[: len(prefix)] == prefix


def _all_folder_paths(
    collection_folders: list[list[str]],
    templates: list[MessageTemplate],
) -> set[tuple[str, ...]]:
    """Every folder path that exists in a collection, including intermediate
    prefixes — both explicit (``Collection.folders``) and the ones implied by
    template ``folder_path`` values."""

    paths: set[tuple[str, ...]] = set()
    sources: list[list[str]] = list(collection_folders or [])
    sources.extend((t.folder_path or []) for t in templates)
    for raw in sources:
        segments = _norm_path(raw)
        for i in range(1, len(segments) + 1):
            paths.add(tuple(segments[:i]))
    return paths


def _distinct_folder_paths(requests: list[Any]) -> list[list[str]]:
    """Unique folder paths (including intermediate prefixes) implied by a set of
    parsed requests, in first-seen order — used to seed ``Collection.folders`` at
    import so the structure (incl. otherwise-empty parent folders) is captured."""

    seen: set[tuple[str, ...]] = set()
    out: list[list[str]] = []
    for request in requests:
        segments = _norm_path(list(getattr(request, "folder_path", []) or []))
        for i in range(1, len(segments) + 1):
            key = tuple(segments[:i])
            if key not in seen:
                seen.add(key)
                out.append(list(key))
    return out


class CollectionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = CollectionRepository(session)
        self.templates = TemplateRepository(session)

    async def list_all(self) -> list[Collection]:
        return await self.repo.list_all()

    async def build_workspace_tree(self, *, search: str = "") -> dict[str, Any]:
        """Build the left-panel tree: collections → folders → templates, plus an
        "ungrouped" bucket for templates without a collection.

        When ``search`` is set, templates are filtered by name/description/URL/
        method/summary and collections with no surviving template are dropped.
        """

        collections = await self.repo.list_all()
        templates = await self.templates.list_all()
        query = search.strip().lower()
        if query:
            templates = [t for t in templates if query in _haystack(t)]

        by_collection: dict[uuid.UUID, list[MessageTemplate]] = {}
        ungrouped: list[MessageTemplate] = []
        for template in templates:
            if template.collection_id is not None:
                by_collection.setdefault(template.collection_id, []).append(template)
            else:
                ungrouped.append(template)

        collection_nodes: list[dict[str, Any]] = []
        for collection in collections:
            items = by_collection.get(collection.id, [])
            if query and not items:
                continue
            collection_nodes.append(
                {
                    "collection": collection,
                    "count": len(items),
                    "tree": build_folder_tree(items, extra_folders=collection.folders),
                }
            )
        return {
            "collection_nodes": collection_nodes,
            "ungrouped_tree": build_folder_tree(ungrouped),
            "ungrouped_count": len(ungrouped),
            "search": search,
        }

    async def get(self, collection_id: uuid.UUID) -> Collection:
        collection = await self.repo.get(collection_id)
        if collection is None:
            raise NotFoundError("Коллекция не найдена")
        return collection

    async def import_postman(self, data: Any) -> ImportCollectionSummary:
        parsed = parse_postman_collection(data)
        collection = Collection(
            name=parsed.name,
            description=parsed.description,
            source=parsed.source,
            source_format=parsed.source_format,
            variables=parsed.variables,
            folders=_distinct_folder_paths(parsed.requests),
        )
        await self.repo.add(collection)

        unparsable = 0
        for order, request in enumerate(parsed.requests):
            if not request.parsable:
                unparsable += 1
            template = MessageTemplate(
                name=request.name or "(без имени)",
                description=request.description,
                format=request.fmt if request.fmt in ("json", "xml") else "json",
                content=request.content,
                original_content=request.content,
                llm_meta={"import_status": "imported" if request.parsable else "unparsed"},
                placeholders=[],
                collection_id=collection.id,
                folder_path=list(request.folder_path),
                headers=apply_dynamic_headers(request.headers),
                http_method=request.http_method,
                url=request.url,
                display_order=order,
            )
            await self.templates.add(template)

        return ImportCollectionSummary(
            collection_id=collection.id,
            name=collection.name,
            templates_created=len(parsed.requests),
            unparsable=unparsable,
        )

    async def delete(self, collection_id: uuid.UUID) -> int:
        collection = await self.get(collection_id)
        removed = await self.templates.delete_by_collection(collection_id)
        await self.repo.delete(collection)
        return removed

    async def create_folder(
        self, collection_id: uuid.UUID, parent_path: list[str], name: str
    ) -> list[str]:
        """Add an (initially empty) folder under ``parent_path``. Persisted on the
        collection so it survives until requests are dropped into it."""

        collection = await self.get(collection_id)
        parent = _norm_path(parent_path)
        clean_name = name.strip()
        if not clean_name:
            raise ValidationFailed("Имя папки не может быть пустым")
        new_path = [*parent, clean_name]
        templates = await self.templates.list_by_collection(collection_id)
        existing = _all_folder_paths(collection.folders, templates)
        if tuple(new_path) in existing:
            raise ValidationFailed("Папка с таким именем уже существует")
        collection.folders = [*collection.folders, new_path]
        await self.session.flush()
        return new_path

    async def rename_folder(
        self, collection_id: uuid.UUID, path: list[str], new_name: str
    ) -> list[str]:
        """Rename the folder at ``path`` to ``new_name``, re-prefixing every
        descendant folder path on both templates and the explicit folder list."""

        collection = await self.get(collection_id)
        old_path = _norm_path(path)
        if not old_path:
            raise ValidationFailed("Не указана папка для переименования")
        clean_name = new_name.strip()
        if not clean_name:
            raise ValidationFailed("Имя папки не может быть пустым")
        new_path = [*old_path[:-1], clean_name]
        if new_path == old_path:
            return new_path

        templates = await self.templates.list_by_collection(collection_id)
        # Collision: a sibling/other folder already occupies the new path. Exclude
        # the folder being renamed and its descendants from the check.
        others = {
            p
            for p in _all_folder_paths(collection.folders, templates)
            if not _starts_with(list(p), old_path)
        }
        if tuple(new_path) in others:
            raise ValidationFailed("Папка с таким именем уже существует")

        for template in templates:
            fp = _norm_path(template.folder_path)
            if _starts_with(fp, old_path):
                template.folder_path = [*new_path, *fp[len(old_path):]]

        updated_folders: list[list[str]] = []
        for raw in collection.folders:
            segments = _norm_path(raw)
            if _starts_with(segments, old_path):
                updated_folders.append([*new_path, *segments[len(old_path):]])
            else:
                updated_folders.append(segments)
        collection.folders = updated_folders
        await self.session.flush()
        return new_path

    async def delete_folder(self, collection_id: uuid.UUID, path: list[str]) -> None:
        """Delete an empty folder. Refuses if any request or sub-folder lives under
        it — the caller must move/remove the contents first."""

        collection = await self.get(collection_id)
        target = _norm_path(path)
        if not target:
            raise ValidationFailed("Не указана папка для удаления")
        templates = await self.templates.list_by_collection(collection_id)
        has_templates = any(
            _starts_with(_norm_path(t.folder_path), target) for t in templates
        )
        has_children = any(
            len(p := _norm_path(raw)) > len(target) and _starts_with(p, target)
            for raw in collection.folders
        )
        if has_templates or has_children:
            raise ValidationFailed(
                "Папка не пуста — сначала переместите или удалите вложенные запросы и папки"
            )
        collection.folders = [
            segments for raw in collection.folders if (segments := _norm_path(raw)) != target
        ]
        await self.session.flush()

    async def move_request(
        self,
        template_id: uuid.UUID,
        target_collection_id: uuid.UUID | None,
        target_folder_path: list[str],
        order: list[uuid.UUID],
    ) -> None:
        """Move a request into ``target_folder_path`` of ``target_collection_id``
        (``None`` = ungrouped) and renumber ``display_order`` for the target
        folder's siblings according to ``order``. Handles intra-folder reorder,
        moves between folders and moves between collections in one call."""

        template = await self.templates.get(template_id)
        if template is None:
            raise NotFoundError("Шаблон не найден")
        if target_collection_id is not None:
            await self.get(target_collection_id)  # 404 if missing

        template.collection_id = target_collection_id
        template.folder_path = _norm_path(target_folder_path)

        if order:
            siblings = {t.id: t for t in await self.templates.get_many(order)}
            for idx, sibling_id in enumerate(order):
                sibling = siblings.get(sibling_id)
                if sibling is not None:
                    sibling.display_order = idx
        await self.session.flush()

    async def process_collection_llm(self, collection_id: uuid.UUID) -> ProcessCollectionSummary:
        """Run LLM analysis across every parsable template of a collection.

        Requires a working LLM: opening :func:`llm_service` raises
        :class:`LLMUnavailable` when it is not configured, which the route turns
        into a user-facing error. Per-template outcomes are counted —
        unparsable bodies are skipped, other failures are recorded.
        """

        await self.get(collection_id)  # 404 if missing
        templates = await self.templates.list_by_collection(collection_id)
        svc = TemplateService(self.session)
        summary = ProcessCollectionSummary()
        async with llm_service() as llm_svc:
            for template in templates:
                try:
                    await svc.analyze_and_persist(template, llm_service=llm_svc)
                    summary.processed += 1
                except ValidationFailed:
                    summary.skipped += 1
                except Exception:
                    log.warning("LLM analysis failed for template %s", template.id, exc_info=True)
                    summary.failed += 1
        return summary
