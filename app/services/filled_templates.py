"""Persist and retrieve rendered (filled) message-template snapshots.

A ``FilledTemplate`` row is a *snapshot* of one fill operation: it stores the
final rendered body plus the set of changed locations (for green highlighting
on view), the list of unresolved tokens (so the UI can flag partial fills),
audit FKs to the upstream template/clients/accounts/cards, and human-readable
``*_snapshot`` strings so the row remains useful after upstream deletes.

Filled templates are organised into arbitrarily nested folders on the
«Заполненные шаблоны» page, mirroring the collections tree: each row carries a
materialised ``folder_path`` and the explicit (possibly empty) folder list
lives in the ``filled_root_folders`` app setting — see the folder methods on
:class:`FilledTemplateService`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FilledTemplate, MessageTemplate
from app.repositories.entity import AccountRepository, CardRepository, ClientRepository
from app.repositories.filled_template import DEFAULT_LIST_LIMIT, FilledTemplateRepository
from app.repositories.request_chain import RequestChainRepository
from app.repositories.settings import SettingsRepository
from app.routes.entities_htmx import entity_label
from app.schemas.template import TemplateFillRequest
from app.services.collections import _new_node, _norm_path, _starts_with, build_folder_tree
from app.services.fill_access import assert_fill_visible
from app.services.placeholders import PlaceholderFiller
from app.services.templates import TemplateService
from app.utils.errors import NotFoundError, ValidationFailed

NAME_MAX_LEN = 255
_ROLES: tuple[str, ...] = ("sender", "receiver", "accountOwner")

# ``AppSetting`` key holding the explicit folder list for filled templates
# (``list[list[str]]``, same shape as ``root_folders`` for message templates).
# Folders created/renamed by the user are persisted here so empty folders
# survive a tree rebuild and rename/delete have an authoritative target.
FILLED_ROOT_FOLDERS_KEY = "filled_root_folders"


def _expand_prefixes(paths: list[list[str]]) -> set[tuple[str, ...]]:
    """Every folder path implied by ``paths``, including intermediate
    prefixes — e.g. ``["A", "B"]`` contributes both ``("A",)`` and
    ``("A", "B")``."""

    out: set[tuple[str, ...]] = set()
    for raw in paths:
        segments = _norm_path(raw)
        for i in range(1, len(segments) + 1):
            out.add(tuple(segments[:i]))
    return out


def _truncate(text: str, *, limit: int = NAME_MAX_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)].rstrip() + "…"


def build_auto_name(
    template_name: str,
    role_labels: dict[str, str],
    now: datetime,
) -> str:
    """Build ``«<template> — <sender> → <receiver> [· <owner>] — DD.MM.YYYY HH:MM»``.

    Roles that have no resolved label are omitted from the middle segment.
    The result is clamped to :data:`NAME_MAX_LEN` characters.
    """

    parts: list[str] = [template_name or "Шаблон"]
    sender = role_labels.get("sender") or ""
    receiver = role_labels.get("receiver") or ""
    owner = role_labels.get("accountOwner") or ""

    middle_bits: list[str] = []
    if sender and receiver:
        middle_bits.append(f"{sender} → {receiver}")
    elif sender:
        middle_bits.append(sender)
    elif receiver:
        middle_bits.append(f"→ {receiver}")
    if owner:
        middle_bits.append(f"владелец: {owner}")
    if middle_bits:
        parts.append(" · ".join(middle_bits))

    parts.append(now.strftime("%d.%m.%Y %H:%M"))
    return _truncate(" — ".join(parts))


def _surname(label: str) -> str:
    """First word of a client label, used as the surname.

    Client data has no dedicated surname field — ``entity_label`` yields a
    display string like ``"Иванов Иван Иванович"`` or ``"ООО Ромашка"``; the
    first token is the surname (or the lead word for orgs). Empty in, empty out.
    """

    cleaned = (label or "").strip()
    return cleaned.split(maxsplit=1)[0] if cleaned else ""


def _role_block(role_bits: dict[str, tuple[str, str]], role: str) -> str:
    """``"<surname> <number>"`` for a role, empty parts dropped; ``""`` if none."""

    surname, number = role_bits.get(role, ("", ""))
    return " ".join(bit for bit in (surname, number) if bit)


def build_short_name(template_name: str, role_bits: dict[str, tuple[str, str]]) -> str:
    """Build ``«<template> <sender> → <receiver> <owner>»``.

    Each role contributes ``"<surname> <account_or_card_number>"`` (empty parts
    dropped). An arrow separates sender and receiver (kept before a lone receiver
    for clarity, omitted for a lone sender). The owner block, if any, trails.
    The result is clamped to :data:`NAME_MAX_LEN`.
    """

    parts: list[str] = [template_name or "Шаблон"]
    sender = _role_block(role_bits, "sender")
    receiver = _role_block(role_bits, "receiver")
    if sender and receiver:
        parts.append(f"{sender} → {receiver}")
    elif sender:
        parts.append(sender)
    elif receiver:
        parts.append(f"→ {receiver}")
    owner = _role_block(role_bits, "accountOwner")
    if owner:
        parts.append(owner)
    return _truncate(" ".join(parts))


@dataclass(frozen=True)
class _RoleIds:
    client_id: uuid.UUID | None
    account_id: uuid.UUID | None
    card_id: uuid.UUID | None


def _role_ids(req: TemplateFillRequest) -> dict[str, _RoleIds]:
    return {
        "sender": _RoleIds(req.sender_client_id, req.sender_account_id, req.sender_card_id),
        "receiver": _RoleIds(req.receiver_client_id, req.receiver_account_id, req.receiver_card_id),
        "accountOwner": _RoleIds(
            req.account_owner_client_id,
            req.account_owner_account_id,
            req.account_owner_card_id,
        ),
    }


async def collect_role_labels(
    session: AsyncSession, req: TemplateFillRequest
) -> dict[str, str]:
    """Build ``{"sender": "Иванов · ACC-001", ...}`` from current entity rows.

    Reuses ``entity_label`` from ``entities_htmx`` so labels match what the
    user sees in client/account pickers. Missing entities are skipped — a
    role with no resolved client returns no entry at all.
    """

    clients = ClientRepository(session)
    accounts = AccountRepository(session)
    cards = CardRepository(session)
    labels: dict[str, str] = {}
    for role, ids in _role_ids(req).items():
        if ids.client_id is None:
            continue
        client = await clients.get(ids.client_id)
        if client is None:
            continue
        bits: list[str] = [entity_label("client", client)]
        if ids.account_id is not None:
            account = await accounts.get(ids.account_id)
            if account is not None:
                bits.append(entity_label("account", account))
        if ids.card_id is not None:
            card = await cards.get(ids.card_id)
            if card is not None:
                bits.append(entity_label("card", card))
        labels[role] = " · ".join(bits)
    return labels


async def collect_role_short_bits(
    session: AsyncSession, req: TemplateFillRequest
) -> dict[str, tuple[str, str]]:
    """Build ``{"sender": ("Иванов", "ACC-001"), ...}`` for :func:`build_short_name`.

    ``surname`` is the first word of the client label; ``number`` is the account
    number if an account was chosen, else the card number, else ``""`` (the
    account and card are mutually exclusive per role in the fill UI). Roles with
    no resolved client are skipped.
    """

    clients = ClientRepository(session)
    accounts = AccountRepository(session)
    cards = CardRepository(session)
    bits: dict[str, tuple[str, str]] = {}
    for role, ids in _role_ids(req).items():
        if ids.client_id is None:
            continue
        client = await clients.get(ids.client_id)
        if client is None:
            continue
        surname = _surname(entity_label("client", client))
        number = ""
        if ids.account_id is not None:
            account = await accounts.get(ids.account_id)
            if account is not None:
                number = entity_label("account", account)
        elif ids.card_id is not None:
            card = await cards.get(ids.card_id)
            if card is not None:
                number = entity_label("card", card)
        bits[role] = (surname, number)
    return bits


async def collect_request_groups(
    session: AsyncSession, req: TemplateFillRequest
) -> dict[uuid.UUID, Any]:
    """Distinct private access groups referenced by ``req``, as ``{group_id: group_row}``.

    Resolves the *owning client* of every referenced entity — a client directly,
    an account via its client, a card via its account's client — so a private
    account/card counts even when the request carries no explicit ``*_client_id``
    (a crafted save could otherwise slip private data through as public). Public
    clients (``group_id is None``) contribute nothing. getattr-safe so test
    doubles without the ``group`` relationship work.
    """

    clients_repo = ClientRepository(session)
    accounts_repo = AccountRepository(session)
    cards_repo = CardRepository(session)

    client_ids: set[uuid.UUID] = set()
    for cid in (req.sender_client_id, req.receiver_client_id, req.account_owner_client_id):
        if cid is not None:
            client_ids.add(cid)
    for aid in (req.sender_account_id, req.receiver_account_id, req.account_owner_account_id):
        if aid is not None:
            account = await accounts_repo.get(aid)
            if account is not None:
                client_ids.add(account.client_id)
    for kid in (req.sender_card_id, req.receiver_card_id, req.account_owner_card_id):
        if kid is not None:
            card = await cards_repo.get(kid)
            if card is not None:
                account = await accounts_repo.get(card.account_id)
                if account is not None:
                    client_ids.add(account.client_id)

    groups: dict[uuid.UUID, Any] = {}  # group_id → group row (for name/color)
    for cid in client_ids:
        client = await clients_repo.get(cid)
        if client is None:
            continue
        gid = getattr(client, "group_id", None)
        if gid is not None:
            groups[gid] = getattr(client, "group", None)
    return groups


# Human-readable role titles, the single source of truth shared by the panel
# (:func:`iter_role_labels`) and the chain management UI.
ROLE_TITLES: dict[str, str] = {
    "sender": "Отправитель",
    "receiver": "Получатель",
    "accountOwner": "Владелец счёта",
}


class FilledTemplateService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = FilledTemplateRepository(session)
        self.settings = SettingsRepository(session)
        # Request chains share this folder tree, so folder create/rename/delete
        # and the tree build must account for their ``folder_path`` values too.
        self.chains = RequestChainRepository(session)

    async def list_all(
        self,
        *,
        search: str = "",
        limit: int = DEFAULT_LIST_LIMIT,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> list[FilledTemplate]:
        return await self.repo.list_all(
            search=search, limit=limit, visible_group_ids=visible_group_ids
        )

    async def get(
        self, filled_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> FilledTemplate:
        item = await self.repo.get(filled_id, visible_group_ids=visible_group_ids)
        if item is None:
            raise NotFoundError("Заполненный шаблон не найден")
        return item

    async def delete(
        self, filled_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> None:
        item = await self.get(filled_id, visible_group_ids=visible_group_ids)
        await self.repo.delete(item)

    # ---- folder tree (mirrors CollectionService, single root namespace) ----
    #
    # Folder checks work on lightweight ``folder_path`` projections instead of
    # full ORM rows: existence/emptiness needs paths only, and rename loads
    # complete rows solely for the descendants it actually re-prefixes — so
    # the operations stay cheap as the table grows.

    async def _explicit_folders(self) -> list[list[str]]:
        return list(await self.settings.get(FILLED_ROOT_FOLDERS_KEY) or [])

    async def _save_folders(self, folders: list[list[str]]) -> None:
        await self.settings.set(FILLED_ROOT_FOLDERS_KEY, folders)
        await self.session.flush()

    async def build_tree(
        self, *, search: str = "", visible_group_ids: set[uuid.UUID] | None = None
    ) -> dict[str, Any]:
        """Build the left-panel tree of folders and filled templates.

        While searching, explicit empty folders are not seeded so the tree
        collapses to actual matches — same behaviour as the collections tree.
        Only filled templates the caller may see (public + unlocked groups) are
        placed in the tree; folders themselves are a shared namespace.
        """

        query = search.strip()
        items = await self.repo.list_all(search=query, visible_group_ids=visible_group_ids)
        explicit = list(await self.settings.get(FILLED_ROOT_FOLDERS_KEY) or [])
        tree = build_folder_tree(items, extra_folders=None if query else explicit)
        chain_count = await self._graft_chains(
            tree, search=query, visible_group_ids=visible_group_ids
        )
        return {
            "tree": tree,
            "count": len(items),
            "chain_count": chain_count,
            "search": search,
            "list_limit": DEFAULT_LIST_LIMIT,
            "truncated": len(items) >= DEFAULT_LIST_LIMIT,
        }

    async def _graft_chains(
        self,
        tree: dict[str, Any],
        *,
        search: str = "",
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> int:
        """Place request chains into the (already built) filled-template tree.

        Each chain is appended to its folder node's ``chains`` list as a
        lightweight dict (the ORM rows' ``steps`` are not loaded, so we never
        touch the lazy relationship here). While searching, chains are filtered
        by name — mirroring how filled templates collapse to matches. Returns the
        number of chains placed.
        """

        chains = await self.chains.list_all(visible_group_ids=visible_group_ids)
        query = search.strip().lower()
        if query:
            chains = [c for c in chains if query in (c.name or "").lower()]
        counts = await self.chains.step_counts()
        ordered = sorted(chains, key=lambda c: (c.display_order, c.created_at))
        for chain in ordered:
            node = tree
            for folder in chain.folder_path or []:
                node = node["folders"].setdefault(str(folder), _new_node())
            node.setdefault("chains", []).append(
                {
                    "id": str(chain.id),
                    "name": chain.name,
                    "step_count": counts.get(chain.id, 0),
                    "group_name_snapshot": getattr(chain, "group_name_snapshot", "") or "",
                    "group_color_snapshot": getattr(chain, "group_color_snapshot", "") or "",
                    "project_name_snapshot": getattr(chain, "project_name_snapshot", "") or "",
                    "project_color_snapshot": getattr(chain, "project_color_snapshot", "") or "",
                }
            )
        return len(ordered)

    async def create_folder(self, parent_path: list[str], name: str) -> list[str]:
        """Add an (initially empty) folder under ``parent_path``."""

        folders = await self._explicit_folders()
        parent = _norm_path(parent_path)
        clean_name = name.strip()
        if not clean_name:
            raise ValidationFailed("Имя папки не может быть пустым")
        new_path = [*parent, clean_name]
        existing = _expand_prefixes(
            [
                *folders,
                *await self.repo.list_folder_paths(),
                *await self.chains.list_folder_paths(),
            ]
        )
        if parent and tuple(parent) not in existing:
            raise ValidationFailed("Родительская папка не найдена")
        if tuple(new_path) in existing:
            raise ValidationFailed("Папка с таким именем уже существует")
        await self._save_folders([*folders, new_path])
        return new_path

    async def rename_folder(self, path: list[str], new_name: str) -> list[str]:
        """Rename the folder at ``path``, re-prefixing every descendant folder
        path on both filled templates and the explicit folder list."""

        folders = await self._explicit_folders()
        pairs = await self.repo.list_ids_with_paths()
        chain_pairs = await self.chains.list_ids_with_paths()
        old_path = _norm_path(path)
        if not old_path:
            raise ValidationFailed("Не указана папка для переименования")
        clean_name = new_name.strip()
        if not clean_name:
            raise ValidationFailed("Имя папки не может быть пустым")
        new_path = [*old_path[:-1], clean_name]

        all_paths = _expand_prefixes(
            [*folders, *[fp for _, fp in pairs], *[fp for _, fp in chain_pairs]]
        )
        # Validate the folder exists *before* the no-op short-circuit, otherwise
        # renaming a missing folder to its own name would falsely report success.
        if tuple(old_path) not in all_paths:
            raise ValidationFailed("Папка не найдена")
        if new_path == old_path:
            return new_path
        # Collision: another folder already occupies the new path. Exclude the
        # folder being renamed and its descendants from the check.
        others = {p for p in all_paths if not _starts_with(list(p), old_path)}
        if tuple(new_path) in others:
            raise ValidationFailed("Папка с таким именем уже существует")

        # Load full rows only for the descendants that actually move.
        descendant_ids = [
            row_id for row_id, fp in pairs if _starts_with(_norm_path(fp), old_path)
        ]
        for item in await self.repo.get_many(descendant_ids):
            fp = _norm_path(item.folder_path)
            item.folder_path = [*new_path, *fp[len(old_path):]]
        # Chains share the namespace, so re-prefix the ones living under the
        # renamed folder too.
        chain_descendant_ids = [
            row_id for row_id, fp in chain_pairs if _starts_with(_norm_path(fp), old_path)
        ]
        for chain in await self.chains.get_many(chain_descendant_ids):
            fp = _norm_path(chain.folder_path)
            chain.folder_path = [*new_path, *fp[len(old_path):]]

        updated_folders: list[list[str]] = []
        for raw in folders:
            segments = _norm_path(raw)
            if _starts_with(segments, old_path):
                updated_folders.append([*new_path, *segments[len(old_path):]])
            else:
                updated_folders.append(segments)
        await self._save_folders(updated_folders)
        return new_path

    async def delete_folder(self, path: list[str]) -> None:
        """Delete an empty folder. Refuses if any filled template or sub-folder
        lives under it — the caller must move/remove the contents first."""

        folders = await self._explicit_folders()
        item_paths = await self.repo.list_folder_paths()
        chain_paths = await self.chains.list_folder_paths()
        target = _norm_path(path)
        if not target:
            raise ValidationFailed("Не указана папка для удаления")
        if tuple(target) not in _expand_prefixes([*folders, *item_paths, *chain_paths]):
            raise ValidationFailed("Папка не найдена")
        has_items = any(
            _starts_with(_norm_path(fp), target) for fp in (*item_paths, *chain_paths)
        )
        has_children = any(
            len(p := _norm_path(raw)) > len(target) and _starts_with(p, target)
            for raw in folders
        )
        if has_items or has_children:
            raise ValidationFailed(
                "Папка не пуста — сначала переместите или удалите вложенные шаблоны и папки"
            )
        await self._save_folders(
            [segments for raw in folders if (segments := _norm_path(raw)) != target]
        )

    async def move_filled(
        self,
        filled_id: uuid.UUID,
        target_folder_path: list[str],
        order: list[uuid.UUID],
        *,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> None:
        """Move a filled template into ``target_folder_path`` and renumber
        ``display_order`` across the target folder's siblings.

        ``order`` carries the ids the client currently *sees* — the tree may
        be filtered by search or truncated, so it can be a subset of the
        folder. Visible items are re-sequenced in payload order across the
        slots they currently occupy; hidden siblings keep their positions.
        The whole folder is renumbered 0..n-1, so a partial payload can never
        produce duplicate ``display_order`` values. Ids that don't belong to
        the folder (crafted or stale payloads) are ignored.
        """

        item = await self.get(filled_id, visible_group_ids=visible_group_ids)
        target_folder = _norm_path(target_folder_path)
        item.folder_path = target_folder
        # The session runs with autoflush=False (app/db/session.py) — flush
        # explicitly so the sibling query below sees the moved item in its new
        # folder; otherwise it would keep a stale display_order that can
        # collide with the renumbered siblings.
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

    async def list_folder_paths(self) -> list[list[str]]:
        """Sorted unique folder paths — feeds the «Сохранить в папку» selector."""

        folders = await self._explicit_folders()
        implied = await self.repo.list_folder_paths()
        chain_implied = await self.chains.list_folder_paths()
        return [
            list(p)
            for p in sorted(_expand_prefixes([*folders, *implied, *chain_implied]))
        ]

    async def _fill_group(
        self, fill_request: TemplateFillRequest
    ) -> tuple[uuid.UUID | None, str, str]:
        """Resolve ``(group_id, name, color)`` for a fill across *all* its roles.

        A saved snapshot is a single artifact with one ``group_id``, so it can
        only safely represent one access group. Over the distinct private groups
        of all referenced clients (see :func:`collect_request_groups`):

        - none → public (``None``);
        - exactly one → that group (name/color snapshotted for the badge);
        - two or more → reject. A single ``group_id`` cannot be hidden from
          holders of group A while shown to holders of group B, so persisting the
          mix would leak one group's data to the other.
        """

        groups = await collect_request_groups(self.session, fill_request)
        if not groups:
            return None, "", ""
        if len(groups) > 1:
            raise ValidationFailed(
                "Нельзя сохранить заполненный шаблон с данными из разных групп доступа — "
                "это раскрыло бы данные одной группы держателям другой. Используйте "
                "данные одной группы (плюс публичные)."
            )
        gid, group = next(iter(groups.items()))
        return gid, getattr(group, "name", "") or "", getattr(group, "color", "") or ""

    async def save_from_fill(
        self,
        *,
        template: MessageTemplate,
        fill_request: TemplateFillRequest,
        rendered: str,
        changed: list[str],
        unresolved: list[str],
        folder_path: list[str] | None = None,
    ) -> FilledTemplate:
        role_labels = await collect_role_labels(self.session, fill_request)
        name = await self._compose_name(template.name, fill_request)
        # getattr-safe: test doubles may not carry the project relationship.
        project = getattr(template, "project", None)
        # The snapshot's access group is derived from all involved role clients
        # (raises on a cross-group mix); name and color are snapshotted so the
        # badge survives the group being deleted.
        group_id, group_name, group_color = await self._fill_group(fill_request)
        target_folder = _norm_path(folder_path)
        item = FilledTemplate(
            name=name,
            format=template.format,
            filled_content=rendered,
            changed_locations=list(changed or []),
            unresolved=list(unresolved or []),
            folder_path=target_folder,
            # Append after the folder's manually ordered siblings — a default
            # of 0 would jump the new row to the top of a sorted folder.
            display_order=await self.repo.next_display_order(target_folder),
            # HTTP request snapshot for the future "send request" feature —
            # copied now so it survives source-template edits and deletes.
            # getattr-safe like ``project`` above.
            http_method_snapshot=(getattr(template, "http_method", "") or "")[:16],
            url_snapshot=getattr(template, "url", "") or "",
            headers_snapshot=list(getattr(template, "headers", []) or []),
            group_id=group_id,
            group_name_snapshot=_truncate(group_name, limit=255),
            group_color_snapshot=group_color,
            message_template_id=template.id,
            template_name_snapshot=_truncate(template.name or "", limit=255),
            project_name_snapshot=_truncate(getattr(project, "name", "") or "", limit=255),
            project_color_snapshot=getattr(project, "color", "") or "",
            sender_client_id=fill_request.sender_client_id,
            sender_account_id=fill_request.sender_account_id,
            sender_card_id=fill_request.sender_card_id,
            receiver_client_id=fill_request.receiver_client_id,
            receiver_account_id=fill_request.receiver_account_id,
            receiver_card_id=fill_request.receiver_card_id,
            account_owner_client_id=fill_request.account_owner_client_id,
            account_owner_account_id=fill_request.account_owner_account_id,
            account_owner_card_id=fill_request.account_owner_card_id,
            role_labels_snapshot=role_labels,
        )
        return await self.repo.add(item)

    async def _compose_name(
        self, template_name: str, fill_request: TemplateFillRequest
    ) -> str:
        """Short name from the live role ids — shared by save and role switch."""

        bits = await collect_role_short_bits(self.session, fill_request)
        return build_short_name(template_name, bits)

    async def switch_role(
        self,
        filled_id: uuid.UUID,
        role: str,
        new_ids: _RoleIds,
        *,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> tuple[FilledTemplate, bool]:
        """Re-point ``role`` to ``new_ids`` and refresh the snapshot.

        Re-renders ``filled_content`` from the source message template, then
        recomputes the role FK columns, ``role_labels_snapshot``, the access
        group, and the short ``name``. Returns ``(item, regenerated)`` where
        ``regenerated`` is ``False`` when the source template was deleted
        (``message_template_id is None``): the body is then left untouched and
        only the roles/labels/group/name are updated.

        Raises ``ValidationFailed`` if the new entities are invisible to the
        caller or mix access groups (the route rolls back, leaving the row
        unchanged).
        """

        if role not in _ROLES:
            raise ValidationFailed("Неизвестная роль")
        if new_ids.client_id is None:
            raise ValidationFailed("Выберите клиента для замены")
        item = await self.get(filled_id, visible_group_ids=visible_group_ids)
        req = _override_role(_fill_request_from_roles(item), role, new_ids)
        await assert_fill_visible(self.session, req, visible_group_ids)

        regenerated = False
        if item.message_template_id is not None:
            template = await TemplateService(self.session).get(item.message_template_id)
            rendered, unresolved, changed = await PlaceholderFiller(self.session).fill_template(
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
            item.filled_content = rendered
            item.unresolved = list(unresolved or [])
            item.changed_locations = list(changed or [])
            regenerated = True

        _apply_roles_to(item, req)
        item.role_labels_snapshot = await collect_role_labels(self.session, req)
        group_id, group_name, group_color = await self._fill_group(req)
        item.group_id = group_id
        item.group_name_snapshot = _truncate(group_name, limit=255)
        item.group_color_snapshot = group_color
        item.name = await self._compose_name(
            item.template_name_snapshot or "", req
        )
        return item, regenerated


# Role-column accessors shared by the filled-template and chain-step switch flows.
# Each role maps to its (client, account, card) FK column names; the same names
# exist on FilledTemplate and RequestChainStep.
_ROLE_COLUMNS: dict[str, tuple[str, str, str]] = {
    "sender": ("sender_client_id", "sender_account_id", "sender_card_id"),
    "receiver": ("receiver_client_id", "receiver_account_id", "receiver_card_id"),
    "accountOwner": (
        "account_owner_client_id",
        "account_owner_account_id",
        "account_owner_card_id",
    ),
}


def _fill_request_from_roles(row: Any) -> TemplateFillRequest:
    """Build a ``TemplateFillRequest`` from a row's current role FK columns."""

    values: dict[str, uuid.UUID | None] = {}
    for client_col, account_col, card_col in _ROLE_COLUMNS.values():
        values[client_col] = getattr(row, client_col, None)
        values[account_col] = getattr(row, account_col, None)
        values[card_col] = getattr(row, card_col, None)
    return TemplateFillRequest(**values)


def _override_role(
    req: TemplateFillRequest, role: str, new_ids: _RoleIds
) -> TemplateFillRequest:
    """Return a copy of ``req`` with ``role``'s triple replaced by ``new_ids``."""

    client_col, account_col, card_col = _ROLE_COLUMNS[role]
    return req.model_copy(
        update={
            client_col: new_ids.client_id,
            account_col: new_ids.account_id,
            card_col: new_ids.card_id,
        }
    )


def _apply_roles_to(row: Any, req: TemplateFillRequest) -> None:
    """Write all nine role FK columns from ``req`` back onto ``row``."""

    for client_col, account_col, card_col in _ROLE_COLUMNS.values():
        setattr(row, client_col, getattr(req, client_col))
        setattr(row, account_col, getattr(req, account_col))
        setattr(row, card_col, getattr(req, card_col))


def role_client_id(item: FilledTemplate, role: str) -> uuid.UUID | None:
    """Helper for templates: current client_id for the named role, if any."""

    mapping: dict[str, Any] = {
        "sender": item.sender_client_id,
        "receiver": item.receiver_client_id,
        "accountOwner": item.account_owner_client_id,
    }
    return mapping.get(role)


def iter_role_labels(item: FilledTemplate) -> list[tuple[str, str, str | None]]:
    """Yield ``(role, title_ru, label)`` for each role with a saved label.

    Order is fixed (sender → receiver → accountOwner). Roles that were never
    filled (no entry in ``role_labels_snapshot``) are skipped.
    """

    snap = item.role_labels_snapshot or {}
    out: list[tuple[str, str, str | None]] = []
    for role in _ROLES:
        if role in snap:
            out.append((role, ROLE_TITLES[role], snap.get(role)))
    return out
