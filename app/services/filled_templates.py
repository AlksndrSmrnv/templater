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
from app.repositories.settings import SettingsRepository
from app.routes.entities_htmx import entity_label
from app.schemas.template import TemplateFillRequest
from app.services.collections import _norm_path, _starts_with, build_folder_tree
from app.utils.errors import NotFoundError, ValidationFailed

NAME_MAX_LEN = 255
_ROLES: tuple[str, ...] = ("sender", "receiver", "accountOwner")

# ``AppSetting`` key holding the explicit folder list for filled templates
# (``list[list[str]]``, same shape as ``root_folders`` for message templates).
# Folders created/renamed by the user are persisted here so empty folders
# survive a tree rebuild and rename/delete have an authoritative target.
FILLED_ROOT_FOLDERS_KEY = "filled_root_folders"


def _all_folder_paths(
    explicit_folders: list[list[str]],
    items: list[FilledTemplate],
) -> set[tuple[str, ...]]:
    """Every folder path that exists, including intermediate prefixes — both
    explicit (the app setting) and the ones implied by item ``folder_path``s."""

    paths: set[tuple[str, ...]] = set()
    sources: list[list[str]] = list(explicit_folders or [])
    sources.extend((item.folder_path or []) for item in items)
    for raw in sources:
        segments = _norm_path(raw)
        for i in range(1, len(segments) + 1):
            paths.add(tuple(segments[:i]))
    return paths


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


class FilledTemplateService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = FilledTemplateRepository(session)
        self.settings = SettingsRepository(session)

    async def list_all(
        self, *, search: str = "", limit: int = DEFAULT_LIST_LIMIT
    ) -> list[FilledTemplate]:
        return await self.repo.list_all(search=search, limit=limit)

    async def get(self, filled_id: uuid.UUID) -> FilledTemplate:
        item = await self.repo.get(filled_id)
        if item is None:
            raise NotFoundError("Заполненный шаблон не найден")
        return item

    async def delete(self, filled_id: uuid.UUID) -> None:
        item = await self.get(filled_id)
        await self.repo.delete(item)

    # ---- folder tree (mirrors CollectionService, single root namespace) ----

    async def _folder_context(self) -> tuple[list[list[str]], list[FilledTemplate]]:
        """Explicit folder list (app setting) plus all filled templates."""

        folders = list(await self.settings.get(FILLED_ROOT_FOLDERS_KEY) or [])
        items = await self.repo.list_all()
        return folders, items

    async def _save_folders(self, folders: list[list[str]]) -> None:
        await self.settings.set(FILLED_ROOT_FOLDERS_KEY, folders)
        await self.session.flush()

    async def build_tree(self, *, search: str = "") -> dict[str, Any]:
        """Build the left-panel tree of folders and filled templates.

        While searching, explicit empty folders are not seeded so the tree
        collapses to actual matches — same behaviour as the collections tree.
        """

        query = search.strip()
        items = await self.repo.list_all(search=query)
        explicit = list(await self.settings.get(FILLED_ROOT_FOLDERS_KEY) or [])
        tree = build_folder_tree(items, extra_folders=None if query else explicit)
        return {
            "tree": tree,
            "count": len(items),
            "search": search,
            "list_limit": DEFAULT_LIST_LIMIT,
            "truncated": len(items) >= DEFAULT_LIST_LIMIT,
        }

    async def create_folder(self, parent_path: list[str], name: str) -> list[str]:
        """Add an (initially empty) folder under ``parent_path``."""

        folders, items = await self._folder_context()
        parent = _norm_path(parent_path)
        clean_name = name.strip()
        if not clean_name:
            raise ValidationFailed("Имя папки не может быть пустым")
        new_path = [*parent, clean_name]
        existing = _all_folder_paths(folders, items)
        if parent and tuple(parent) not in existing:
            raise ValidationFailed("Родительская папка не найдена")
        if tuple(new_path) in existing:
            raise ValidationFailed("Папка с таким именем уже существует")
        await self._save_folders([*folders, new_path])
        return new_path

    async def rename_folder(self, path: list[str], new_name: str) -> list[str]:
        """Rename the folder at ``path``, re-prefixing every descendant folder
        path on both filled templates and the explicit folder list."""

        folders, items = await self._folder_context()
        old_path = _norm_path(path)
        if not old_path:
            raise ValidationFailed("Не указана папка для переименования")
        clean_name = new_name.strip()
        if not clean_name:
            raise ValidationFailed("Имя папки не может быть пустым")
        new_path = [*old_path[:-1], clean_name]

        all_paths = _all_folder_paths(folders, items)
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

        for item in items:
            fp = _norm_path(item.folder_path)
            if _starts_with(fp, old_path):
                item.folder_path = [*new_path, *fp[len(old_path):]]

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

        folders, items = await self._folder_context()
        target = _norm_path(path)
        if not target:
            raise ValidationFailed("Не указана папка для удаления")
        if tuple(target) not in _all_folder_paths(folders, items):
            raise ValidationFailed("Папка не найдена")
        has_items = any(
            _starts_with(_norm_path(item.folder_path), target) for item in items
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
    ) -> None:
        """Move a filled template into ``target_folder_path`` and renumber
        ``display_order`` for the target folder's siblings according to
        ``order``. Handles intra-folder reorder and moves between folders."""

        item = await self.get(filled_id)
        target_folder = _norm_path(target_folder_path)
        item.folder_path = target_folder

        # Renumber only across rows that actually live in the target folder
        # (after the move) — a crafted or stale ``order`` payload must not
        # reshuffle unrelated rows elsewhere.
        if order:
            siblings = {row.id: row for row in await self.repo.get_many(order)}
            position = 0
            for sibling_id in order:
                sibling = siblings.get(sibling_id)
                if sibling is None:
                    continue
                if _norm_path(sibling.folder_path) != target_folder:
                    continue
                sibling.display_order = position
                position += 1
        await self.session.flush()

    async def list_folder_paths(self) -> list[list[str]]:
        """Sorted unique folder paths — feeds the «Сохранить в папку» selector."""

        folders, items = await self._folder_context()
        return [list(p) for p in sorted(_all_folder_paths(folders, items))]

    async def save_from_fill(
        self,
        *,
        template: MessageTemplate,
        fill_request: TemplateFillRequest,
        rendered: str,
        changed: list[str],
        unresolved: list[str],
        folder_path: list[str] | None = None,
        now: datetime | None = None,
    ) -> FilledTemplate:
        role_labels = await collect_role_labels(self.session, fill_request)
        moment = now or datetime.utcnow()
        name = build_auto_name(template.name, role_labels, moment)
        # getattr-safe: test doubles may not carry the project relationship.
        project = getattr(template, "project", None)
        item = FilledTemplate(
            name=name,
            format=template.format,
            filled_content=rendered,
            changed_locations=list(changed or []),
            unresolved=list(unresolved or []),
            folder_path=_norm_path(folder_path),
            # HTTP request snapshot for the future "send request" feature —
            # copied now so it survives source-template edits and deletes.
            # getattr-safe like ``project`` above.
            http_method_snapshot=(getattr(template, "http_method", "") or "")[:16],
            url_snapshot=getattr(template, "url", "") or "",
            headers_snapshot=list(getattr(template, "headers", []) or []),
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

    titles = {"sender": "Отправитель", "receiver": "Получатель", "accountOwner": "Владелец счёта"}
    snap = item.role_labels_snapshot or {}
    out: list[tuple[str, str, str | None]] = []
    for role in _ROLES:
        if role in snap:
            out.append((role, titles[role], snap.get(role)))
    return out
