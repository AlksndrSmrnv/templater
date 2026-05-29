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

from app.config import get_settings
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


def build_folder_tree(templates: list[MessageTemplate]) -> dict[str, Any]:
    """Group templates into a nested ``{"folders": {...}, "templates": [...]}``.

    Folders come from each template's materialised ``folder_path``. Order within
    a level follows ``display_order`` then ``created_at`` (preserving import
    order). Consumed by ``partials/collections_tree.html`` via a recursive macro.
    """

    root = _new_node()
    ordered = sorted(templates, key=lambda t: (t.display_order, t.created_at))
    for template in ordered:
        node = root
        for folder in template.folder_path or []:
            node = node["folders"].setdefault(str(folder), _new_node())
        node["templates"].append(template)
    return root


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
                    "tree": build_folder_tree(items),
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

    async def process_collection_llm(self, collection_id: uuid.UUID) -> ProcessCollectionSummary:
        await self.get(collection_id)  # 404 if missing
        templates = await self.templates.list_by_collection(collection_id)
        svc = TemplateService(self.session)
        summary = ProcessCollectionSummary()
        if get_settings().llm_active:
            try:
                async with llm_service() as llm_svc:
                    for template in templates:
                        await self._process_one(svc, template, llm_svc, summary)
            except Exception:
                # LLM context (cert decode / connect) failed for the whole batch —
                # degrade to heuristic so the import still gets dynamic params.
                log.warning("LLM unavailable for batch; heuristic fallback", exc_info=True)
                for template in templates:
                    await self._process_one(svc, template, None, summary)
        else:
            for template in templates:
                await self._process_one(svc, template, None, summary)
        return summary

    async def _process_one(
        self,
        svc: TemplateService,
        template: MessageTemplate,
        llm_svc: Any | None,
        summary: ProcessCollectionSummary,
    ) -> None:
        try:
            try:
                await svc.analyze_and_persist(template, llm_service=llm_svc)
            except ValidationFailed:
                raise
            except Exception:
                # Per-template LLM hiccup → retry heuristically so one bad
                # request doesn't abort the batch.
                log.warning(
                    "LLM analysis failed for template %s; heuristic fallback",
                    template.id,
                    exc_info=True,
                )
                await svc.analyze_and_persist(template, llm_service=None)
            summary.processed += 1
        except ValidationFailed:
            summary.skipped += 1
        except Exception:
            log.warning("Processing failed for template %s", template.id, exc_info=True)
            summary.failed += 1
