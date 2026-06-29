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
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RequestChain, RequestChainStep
from app.repositories.filled_template import FilledTemplateRepository
from app.repositories.request_chain import RequestChainRepository
from app.repositories.settings import SettingsRepository
from app.services.collections import _norm_path
from app.services.fill_access import assert_fill_visible
from app.services.filled_templates import (
    _ROLE_COLUMNS,
    FILLED_ROOT_FOLDERS_KEY,
    _apply_roles_to,
    _expand_prefixes,
    _fill_request_from_roles,
    _override_role,
    _RoleIds,
    build_short_name,
    collect_request_groups,
    collect_role_labels,
    collect_role_short_bits,
)
from app.services.placeholders import PlaceholderFiller
from app.services.templates import TemplateService
from app.utils import walker
from app.utils.errors import NotFoundError, ValidationFailed

NAME_MAX_LEN = 255

# Shared wording for the chain's single-group invariant — used both when adding a
# step and when recomputing the group after a client switch, so the user sees the
# same message regardless of how the conflict arose.
_CROSS_GROUP_MSG = (
    "Нельзя смешивать в одной цепочке данные из разных групп доступа — "
    "это раскрыло бы данные одной группы держателям другой."
)

# Shown when a step's project differs from the chain's. Unlike groups, the rule
# is strict: a chain is locked to the project of its first step (including «no
# project»), so every later step must come from that same project.
_CROSS_PROJECT_MSG = (
    "Все шаги цепочки должны быть из одного проекта — этот шаблон из другого "
    "проекта."
)


def _leaf_exists(fmt: str, body: str, location: str) -> bool:
    """Whether a *replaceable* leaf at ``location`` is present in ``body``.

    The document root (``""``/``"/"``) is excluded: ``walker.replace_*`` can't
    set the root, so a bare-scalar body has no bindable field — reporting it as
    present would buffer an original and then no-op the replace."""

    if location in ("", "/"):
        return False
    try:
        if fmt == "json":
            leaves = walker.walk_json(body)
        elif fmt == "xml":
            leaves = walker.walk_xml(body)
        else:
            return False
    except Exception:
        return False
    return any(leaf.location == location for leaf in leaves)


def _original_leaf(fmt: str, body: str, location: str) -> Any:
    """The leaf's *typed* value (so a JSON number round-trips as a number on
    reset, not a string), or ``None`` if the body doesn't parse / location is
    absent. XML is text-only by nature."""

    if fmt == "json":
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return None
        node: Any = data
        for tok in location.lstrip("/").split("/"):
            tok = tok.replace("~1", "/").replace("~0", "~")
            try:
                node = node[int(tok)] if isinstance(node, list) else node[tok]
            except (KeyError, IndexError, ValueError, TypeError):
                return None
        return node
    if fmt == "xml":
        try:
            leaves = walker.walk_xml(body)
        except Exception:
            return None
        return next((leaf.value for leaf in leaves if leaf.location == location), None)
    return None


def _replace_leaf(fmt: str, body: str, location: str, new_value: Any) -> str:
    """Return ``body`` with the leaf at ``location`` set to ``new_value``.

    ``new_value`` keeps its Python type for JSON (so resetting a number restores
    a number); only ``json``/``xml`` bodies are supported."""

    if fmt == "json":
        return walker.replace_json(body, {location: new_value})
    if fmt == "xml":
        return walker.replace_xml(body, {location: str(new_value)})
    raise ValidationFailed("Неподдерживаемый формат тела для привязки поля")


def default_mock_response(now: datetime | None = None) -> str:
    """A realistic JSON example response seeded onto a new step.

    Editable by the user; the stub send echoes it back so the chain can
    demonstrate pulling fields (e.g. ``transferId``) into later requests.
    """

    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return json.dumps(
        {
            "statusCode": 0,
            "status": "SUCCESS",
            "transferId": f"TRF-{random.randint(100000, 999999)}",
            "processedAt": moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
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

    async def move_chain(
        self,
        chain_id: uuid.UUID,
        target_folder_path: list[str],
        order: list[uuid.UUID],
        *,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> None:
        """Move a chain into ``target_folder_path`` and renumber ``display_order``
        across the target folder's chain siblings.

        Mirrors ``FilledTemplateService.move_filled``: chains have their own
        ``display_order`` sequence (independent of filled templates — in the tree
        they render after the templates of a folder), so only chain siblings are
        renumbered. ``order`` is the ids the client sees (may be a filtered
        subset); visible chains are re-sequenced across the slots they occupy and
        the whole folder is renumbered 0..n-1, so a partial payload can't produce
        duplicate orders. Unknown ids are ignored.
        """

        chain = await self.get(chain_id, visible_group_ids=visible_group_ids)
        target_folder = _norm_path(target_folder_path)
        chain.folder_path = target_folder
        # autoflush=False — flush so the sibling query sees the moved chain in its
        # new folder (else a stale display_order could collide on renumber).
        await self.session.flush()

        siblings = await self.repo.list_by_folder(target_folder)
        full = sorted(siblings, key=lambda row: (row.display_order, row.created_at))
        by_id = {row.id: row for row in full}
        payload: list[uuid.UUID] = []
        payload_ids: set[uuid.UUID] = set()
        for raw_id in order:
            if raw_id in by_id and raw_id not in payload_ids:
                payload.append(raw_id)
                payload_ids.add(raw_id)
        payload_iter = iter(payload)
        resequenced = (
            by_id[next(payload_iter)] if row.id in payload_ids else row for row in full
        )
        for position, row in enumerate(resequenced):
            row.display_order = position
        await self.session.flush()

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
        carries a single ``group_id`` (visibility), resolved from the steps:

        - a **public** filled template (``group_id is None``) may be added to any
          chain, including a private one. This is intentional and safe: it never
          *widens* access — the public copy inherits the chain's stricter
          visibility, it can't leak a private group's data. So we don't touch the
          chain's group for public steps.
        - the **first private** filled template sets the chain's group;
        - a filled template from a **different** private group is rejected — a
          lone ``group_id`` cannot hide one group's data from holders of another.
        """

        chain = await self.get(chain_id, visible_group_ids=visible_group_ids)
        filled = await self.filled.get(filled_id, visible_group_ids=visible_group_ids)
        if filled is None:
            raise NotFoundError("Заполненный шаблон не найден")

        # NB: add_step derives the chain group from the filled template's group
        # *snapshot* (taken at fill time), whereas ``_apply_chain_group`` (run on a
        # later client switch) recomputes from the clients' *current* groups. The
        # snapshot can lag if a client was moved between groups after the fill was
        # saved — that's the long-standing snapshot contract; a switch reconciles it.
        filled_group = getattr(filled, "group_id", None)
        # Public steps (filled_group is None) fall through untouched — see the
        # docstring: allowing them into a private chain only narrows visibility.
        if filled_group is not None:
            if chain.group_id is None:
                chain.group_id = filled_group
                chain.group_name_snapshot = getattr(filled, "group_name_snapshot", "") or ""
                chain.group_color_snapshot = getattr(filled, "group_color_snapshot", "") or ""
            elif chain.group_id != filled_group:
                raise ValidationFailed(_CROSS_GROUP_MSG)

        # Project invariant (strict): the first step locks the chain to its
        # project (by name snapshot — there is no project_id); every later step
        # must match it, «no project» («") included. ``chain.steps`` is the
        # already-loaded collection (the new step isn't added yet), so a
        # non-empty chain is one that already has the lock set.
        filled_project = getattr(filled, "project_name_snapshot", "") or ""
        if chain.steps:
            if filled_project != (chain.project_name_snapshot or ""):
                raise ValidationFailed(_CROSS_PROJECT_MSG)
        else:
            chain.project_name_snapshot = filled_project
            chain.project_color_snapshot = getattr(filled, "project_color_snapshot", "") or ""

        step = RequestChainStep(
            chain_id=chain.id,
            position=await self.repo.next_position(chain.id),
            filled_template_id=filled.id,
            # Role bindings copied from the source so «Заменить клиента» can later
            # re-point a role and re-render this step's body. getattr-safe for the
            # lightweight test doubles used in the service tests.
            sender_client_id=getattr(filled, "sender_client_id", None),
            sender_account_id=getattr(filled, "sender_account_id", None),
            sender_card_id=getattr(filled, "sender_card_id", None),
            receiver_client_id=getattr(filled, "receiver_client_id", None),
            receiver_account_id=getattr(filled, "receiver_account_id", None),
            receiver_card_id=getattr(filled, "receiver_card_id", None),
            account_owner_client_id=getattr(filled, "account_owner_client_id", None),
            account_owner_account_id=getattr(filled, "account_owner_account_id", None),
            account_owner_card_id=getattr(filled, "account_owner_card_id", None),
            role_labels_snapshot=dict(getattr(filled, "role_labels_snapshot", {}) or {}),
            name_snapshot=(getattr(filled, "name", "") or "")[:NAME_MAX_LEN],
            format=getattr(filled, "format", "json") or "json",
            http_method_snapshot=(getattr(filled, "http_method_snapshot", "") or "")[:16],
            url_snapshot=getattr(filled, "url_snapshot", "") or "",
            headers_snapshot=list(getattr(filled, "headers_snapshot", []) or []),
            body=getattr(filled, "filled_content", "") or "",
            mock_response=default_mock_response(),
            # Green-colour markers in the chain UI: the locations this filled
            # template replaced with concrete test data. The other colours
            # (blue dynamic tokens, purple references, white literals) derive
            # from the body text itself.
            changed_locations=list(getattr(filled, "changed_locations", []) or []),
            bindings={},
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
        chain = await self.get(chain_id, visible_group_ids=visible_group_ids)
        step = await self._get_step(chain_id, step_id, visible_group_ids=visible_group_ids)
        await self.repo.delete_step(step)
        # Renumber remaining steps so positions stay 0..n-1 (no gaps).
        await self.session.flush()
        remaining = await self.repo.list_steps(chain_id)
        for position, row in enumerate(remaining):
            row.position = position
        # Last step gone → drop the project lock so the now-empty chain can be
        # re-seeded with any project on the next add.
        if not remaining:
            chain.project_name_snapshot = ""
            chain.project_color_snapshot = ""

    async def bind_field(
        self,
        chain_id: uuid.UUID,
        step_id: uuid.UUID,
        *,
        location: str,
        ref_step: int,
        ref_path: str,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> RequestChainStep:
        """Bind the leaf at ``location`` to ``{{ $ref_step.ref_path }}``.

        The reference lives inline in ``body`` (so send/resolve/dependency logic
        keeps reading it from there); the leaf's pre-bind value is buffered in
        ``bindings`` once, so «Сбросить» can restore the original literal even
        after the source is re-bound to a different field."""

        ref_path = (ref_path or "").strip()
        if not ref_path:
            raise ValidationFailed("Некорректная ссылка на поле ответа")
        step = await self._get_step(chain_id, step_id, visible_group_ids=visible_group_ids)
        # A reference may only point at an *earlier* step (1-based ≤ this step's
        # 0-based position). The UI already restricts this; enforce it server-side
        # so a forged request can't store an unresolvable forward/self reference.
        if not 1 <= ref_step <= step.position:
            raise ValidationFailed("Ссылка может указывать только на предыдущий шаг")
        if not _leaf_exists(step.format, step.body, location):
            raise NotFoundError("Поле не найдено в теле запроса")
        # Remember the first (true literal/dynamic) value only — a re-bind must
        # not overwrite it with a previous reference token. Stored typed so a
        # number resets to a number, not a string.
        if location not in (step.bindings or {}):
            original = _original_leaf(step.format, step.body, location)
            step.bindings = {**(step.bindings or {}), location: original}
        token = f"{{{{ ${ref_step}.{ref_path} }}}}"
        step.body = _replace_leaf(step.format, step.body, location, token)
        return step

    async def unbind_field(
        self,
        chain_id: uuid.UUID,
        step_id: uuid.UUID,
        *,
        location: str,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> RequestChainStep:
        """Restore the leaf at ``location`` to its buffered pre-bind value."""

        step = await self._get_step(chain_id, step_id, visible_group_ids=visible_group_ids)
        bindings = dict(step.bindings or {})
        if location not in bindings:
            raise NotFoundError("Это поле не привязано к ответу")
        step.body = _replace_leaf(step.format, step.body, location, bindings.pop(location))
        step.bindings = bindings
        return step

    # ---- client switching ----

    async def _rerender_step(
        self,
        step: RequestChainStep,
        overrides: dict[str, _RoleIds],
        *,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> bool:
        """Re-point one or more roles of ``step`` and re-render its body.

        ``overrides`` maps role → new ids; all are applied in a single re-render
        so a step whose sender and receiver are the same client never passes
        through a mixed (one-new-one-old) intermediate body. Rebuilds ``body``
        from the source message template (reached via ``filled_template_id`` →
        ``message_template_id``) with the step's role ids. Existing ``{{ $N.path }}``
        field bindings are re-applied where their leaf still exists in the new
        body (the reset buffer is refreshed to the new literal); bindings whose
        leaf vanished are dropped. ``changed_locations`` is reset to the fresh
        fill. Returns whether the body was regenerated — ``False`` when the
        source template/filled template is unreachable, in which case only the
        role columns and labels are updated.
        """

        req = _fill_request_from_roles(step)
        for role, new_ids in overrides.items():
            if role not in _ROLE_COLUMNS:
                raise ValidationFailed("Неизвестная роль")
            req = _override_role(req, role, new_ids)
        await assert_fill_visible(self.session, req, visible_group_ids)

        # step → filled template → message template; either hop may be NULL
        # (ON DELETE SET NULL), in which case we can't re-render the body.
        filled = (
            await self.filled.get(step.filled_template_id)
            if step.filled_template_id is not None
            else None
        )
        template = None
        mtid = getattr(filled, "message_template_id", None) if filled is not None else None
        if mtid is not None:
            template = await TemplateService(self.session).get(mtid)

        regenerated = False
        if template is not None:
            # Snapshot active reference tokens before the re-render wipes them.
            preserved = {
                loc: _original_leaf(step.format, step.body, loc)
                for loc in (step.bindings or {})
            }
            rendered, _unresolved, changed = await PlaceholderFiller(self.session).fill_template(
                template,
                sender_client_id=req.sender_client_id,
                sender_account_id=req.sender_account_id,
                sender_card_id=req.sender_card_id,
                receiver_client_id=req.receiver_client_id,
                receiver_account_id=req.receiver_account_id,
                receiver_card_id=req.receiver_card_id,
                account_owner_client_id=req.account_owner_client_id,
                account_owner_account_id=req.account_owner_account_id,
                account_owner_card_id=req.account_owner_card_id,
            )
            new_bindings: dict[str, Any] = {}
            for loc, token in preserved.items():
                if not _leaf_exists(step.format, rendered, loc):
                    continue  # leaf gone after re-render — drop the stale binding
                # Refresh the reset buffer to the new client's literal, then
                # overlay the reference token so the binding survives.
                new_bindings[loc] = _original_leaf(step.format, rendered, loc)
                rendered = _replace_leaf(step.format, rendered, loc, token)
            step.body = rendered
            step.bindings = new_bindings
            step.changed_locations = list(changed or [])
            regenerated = True

        _apply_roles_to(step, req)
        step.role_labels_snapshot = await collect_role_labels(self.session, req)
        if filled is not None:
            template_name = getattr(filled, "template_name_snapshot", "") or ""
            bits = await collect_role_short_bits(self.session, req)
            step.name_snapshot = build_short_name(template_name, bits)[:NAME_MAX_LEN]
        return regenerated

    async def _apply_chain_group(self, chain: RequestChain) -> None:
        """Recompute ``chain.group_id`` from the role clients of *all* its steps.

        A chain carries a single ``group_id``; after a client switch the minimal
        access group is whatever the steps now reference. Mixing two private
        groups is rejected (a lone ``group_id`` can't hide one group's data from
        holders of another). Resolving to ``None`` when nothing is private is
        safe — the chain holds only public data. Mirrors ``add_step``'s invariant
        but recomputed from scratch so a switch can't leave a stale group.
        """

        groups: dict[uuid.UUID, Any] = {}
        for step in chain.steps:
            groups.update(
                await collect_request_groups(self.session, _fill_request_from_roles(step))
            )
        if len(groups) > 1:
            raise ValidationFailed(_CROSS_GROUP_MSG)
        if not groups:
            chain.group_id = None
            chain.group_name_snapshot = ""
            chain.group_color_snapshot = ""
            return
        gid, group = next(iter(groups.items()))
        chain.group_id = gid
        chain.group_name_snapshot = (getattr(group, "name", "") or "")[:NAME_MAX_LEN]
        chain.group_color_snapshot = getattr(group, "color", "") or ""

    async def switch_step_client(
        self,
        chain_id: uuid.UUID,
        step_id: uuid.UUID,
        role: str,
        new_ids: _RoleIds,
        *,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> tuple[RequestChainStep, bool]:
        """Switch one role of one step. Returns ``(step, regenerated)``."""

        if new_ids.client_id is None:
            raise ValidationFailed("Выберите клиента для замены")
        # ``get`` already loads chain.steps; take the step from there (rather than
        # a second ``_get_step`` query) so the mutated object is exactly the one
        # ``_apply_chain_group`` will re-scan below.
        chain = await self.get(chain_id, visible_group_ids=visible_group_ids)
        step = next((s for s in chain.steps if s.id == step_id), None)
        if step is None:
            raise NotFoundError("Шаг не найден")
        regenerated = await self._rerender_step(
            step, {role: new_ids}, visible_group_ids=visible_group_ids
        )
        # Recompute the chain's access group so a switch into another group can't
        # leave private data under the old (now wrong) group_id — a cross-group
        # mix is rejected, rolling back the whole switch.
        await self._apply_chain_group(chain)
        return step, regenerated

    async def replace_role_everywhere(
        self,
        chain_id: uuid.UUID,
        role: str,
        new_ids: _RoleIds,
        *,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> tuple[list[RequestChainStep], int]:
        """Re-point ``role`` to ``new_ids`` on *every* step that has it, re-rendering
        each. «Заменить отправителя/получателя/владельца во всей цепочке».

        Only steps where the role is populated (``<role>_client_id is not None``)
        are touched — a step whose template doesn't use the role keeps it empty.
        Returns ``(changed, regenerated)``: the list of steps whose role was
        re-pointed, and how many of those had their body actually re-rendered
        (steps whose source template was deleted update the role/labels only). An
        empty list means no step has the role.
        """

        if role not in _ROLE_COLUMNS:
            raise ValidationFailed("Неизвестная роль")
        if new_ids.client_id is None:
            raise ValidationFailed("Выберите клиента для замены")
        client_col = _ROLE_COLUMNS[role][0]
        chain = await self.get(chain_id, visible_group_ids=visible_group_ids)
        changed: list[RequestChainStep] = []
        regenerated = 0
        for step in chain.steps:
            if getattr(step, client_col) is None:
                continue
            if await self._rerender_step(
                step, {role: new_ids}, visible_group_ids=visible_group_ids
            ):
                regenerated += 1
            changed.append(step)
        if changed:
            await self._apply_chain_group(chain)
        return changed, regenerated

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
