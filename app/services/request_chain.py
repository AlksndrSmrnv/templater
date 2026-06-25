"""Business logic for «Цепочка запросов» — ordered chains of REST requests.

A chain is assembled from existing «Заполненные шаблоны»: each step snapshots
one filled template's request envelope (so it stays runnable after the source
changes), carries an editable example response (``mock_response``), and may
reference fields of earlier steps' responses via ``{{ $N.path }}`` tokens stored
inline in its ``body``. There is no real sending yet — a stub seam echoes the
example response (see ``app/routes/chains.py``).

Chains live in the same folder tree as filled templates; folder create/rename/
delete therefore account for chain ``folder_path`` values too — that integration
lives on :class:`FilledTemplateService`, which owns the tree.
"""

from __future__ import annotations

import json
import random
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RequestChain, RequestChainStep
from app.repositories.filled_template import FilledTemplateRepository
from app.repositories.request_chain import RequestChainRepository
from app.repositories.settings import SettingsRepository
from app.services.collections import _norm_path
from app.services.filled_templates import FILLED_ROOT_FOLDERS_KEY, _expand_prefixes
from app.utils.errors import NotFoundError, ValidationFailed

NAME_MAX_LEN = 255


def default_mock_response(now: datetime | None = None) -> str:
    """A realistic JSON example response seeded onto a new step.

    Editable by the user; the stub send echoes it back so the chain can
    demonstrate pulling fields (e.g. ``transferId``) into later requests.
    """

    moment = now or datetime.utcnow()
    return json.dumps(
        {
            "status": "SUCCESS",
            "transferId": f"TRF-{random.randint(100000, 999999)}",
            "processedAt": moment.replace(microsecond=0).isoformat() + "Z",
        },
        ensure_ascii=False,
        indent=2,
    )


class RequestChainService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = RequestChainRepository(session)
        self.filled = FilledTemplateRepository(session)
        self.settings = SettingsRepository(session)

    # ---- chain CRUD ----

    async def get(
        self, chain_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> RequestChain:
        chain = await self.repo.get(chain_id, visible_group_ids=visible_group_ids)
        if chain is None:
            raise NotFoundError("Цепочка не найдена")
        return chain

    async def _known_folders(self) -> set[tuple[str, ...]]:
        explicit = list(await self.settings.get(FILLED_ROOT_FOLDERS_KEY) or [])
        filled_paths = await self.filled.list_folder_paths()
        chain_paths = await self.repo.list_folder_paths()
        return _expand_prefixes([*explicit, *filled_paths, *chain_paths])

    async def create_chain(self, parent_path: list[str], name: str) -> RequestChain:
        """Create an empty chain under ``parent_path`` (root = ``[]``)."""

        clean_name = name.strip()
        if not clean_name:
            raise ValidationFailed("Имя цепочки не может быть пустым")
        parent = _norm_path(parent_path)
        if parent and tuple(parent) not in await self._known_folders():
            raise ValidationFailed("Папка не найдена")
        chain = RequestChain(
            name=clean_name[:NAME_MAX_LEN],
            folder_path=parent,
            display_order=await self.repo.next_display_order(parent),
        )
        return await self.repo.add(chain)

    async def rename_chain(
        self,
        chain_id: uuid.UUID,
        new_name: str,
        *,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> RequestChain:
        clean_name = new_name.strip()
        if not clean_name:
            raise ValidationFailed("Имя цепочки не может быть пустым")
        chain = await self.get(chain_id, visible_group_ids=visible_group_ids)
        chain.name = clean_name[:NAME_MAX_LEN]
        return chain

    async def delete_chain(
        self, chain_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> None:
        chain = await self.get(chain_id, visible_group_ids=visible_group_ids)
        # Steps cascade (FK ON DELETE CASCADE + relationship cascade).
        await self.repo.delete(chain)

    # ---- steps ----

    async def add_step(
        self,
        chain_id: uuid.UUID,
        filled_id: uuid.UUID,
        *,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> RequestChainStep:
        """Append a filled template to the chain as a new step.

        The request envelope is snapshotted from the filled template. A chain
        carries a single ``group_id``: a public filled template adds freely; the
        first private-group filled template sets the chain's group, and a
        conflicting group is rejected (a lone ``group_id`` can't hide one group's
        data from holders of another).
        """

        chain = await self.get(chain_id, visible_group_ids=visible_group_ids)
        filled = await self.filled.get(filled_id, visible_group_ids=visible_group_ids)
        if filled is None:
            raise NotFoundError("Заполненный шаблон не найден")

        filled_group = getattr(filled, "group_id", None)
        if filled_group is not None:
            if chain.group_id is None:
                chain.group_id = filled_group
                chain.group_name_snapshot = getattr(filled, "group_name_snapshot", "") or ""
                chain.group_color_snapshot = getattr(filled, "group_color_snapshot", "") or ""
            elif chain.group_id != filled_group:
                raise ValidationFailed(
                    "Нельзя смешивать в одной цепочке шаги из разных групп доступа"
                )

        step = RequestChainStep(
            chain_id=chain.id,
            position=await self.repo.next_position(chain.id),
            filled_template_id=filled.id,
            name_snapshot=(getattr(filled, "name", "") or "")[:NAME_MAX_LEN],
            format=getattr(filled, "format", "json") or "json",
            http_method_snapshot=(getattr(filled, "http_method_snapshot", "") or "")[:16],
            url_snapshot=getattr(filled, "url_snapshot", "") or "",
            headers_snapshot=list(getattr(filled, "headers_snapshot", []) or []),
            body=getattr(filled, "filled_content", "") or "",
            mock_response=default_mock_response(),
        )
        return await self.repo.add_step(step)

    async def _get_step(
        self,
        chain_id: uuid.UUID,
        step_id: uuid.UUID,
        *,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> RequestChainStep:
        # Resolve the chain first so visibility is enforced before touching steps.
        await self.get(chain_id, visible_group_ids=visible_group_ids)
        step = await self.repo.get_step(step_id)
        if step is None or step.chain_id != chain_id:
            raise NotFoundError("Шаг не найден")
        return step

    async def remove_step(
        self,
        chain_id: uuid.UUID,
        step_id: uuid.UUID,
        *,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> None:
        step = await self._get_step(chain_id, step_id, visible_group_ids=visible_group_ids)
        await self.repo.delete_step(step)
        # Renumber remaining steps so positions stay 0..n-1 (no gaps).
        await self.session.flush()
        for position, remaining in enumerate(await self.repo.list_steps(chain_id)):
            remaining.position = position

    async def update_step(
        self,
        chain_id: uuid.UUID,
        step_id: uuid.UUID,
        *,
        body: str | None = None,
        mock_response: str | None = None,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> RequestChainStep:
        step = await self._get_step(chain_id, step_id, visible_group_ids=visible_group_ids)
        if body is not None:
            step.body = body
        if mock_response is not None:
            step.mock_response = mock_response
        return step

    async def reorder_steps(
        self,
        chain_id: uuid.UUID,
        order: list[uuid.UUID],
        *,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> None:
        """Renumber ``position`` to follow ``order``. Ids not in the chain are
        ignored; steps missing from ``order`` keep their relative order after the
        listed ones, so the chain always ends up numbered 0..n-1 without gaps."""

        chain = await self.get(chain_id, visible_group_ids=visible_group_ids)
        full = sorted(chain.steps, key=lambda s: (s.position, s.created_at))
        by_id = {s.id: s for s in full}
        wanted: list[uuid.UUID] = []
        seen: set[uuid.UUID] = set()
        for raw_id in order:
            if raw_id in by_id and raw_id not in seen:
                wanted.append(raw_id)
                seen.add(raw_id)
        ordered = [by_id[i] for i in wanted] + [s for s in full if s.id not in seen]
        for position, step in enumerate(ordered):
            step.position = position
