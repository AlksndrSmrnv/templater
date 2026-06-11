"""Persist and retrieve rendered (filled) message-template snapshots.

A ``FilledTemplate`` row is a *snapshot* of one fill operation: it stores the
final rendered body plus the set of changed locations (for green highlighting
on view), the list of unresolved tokens (so the UI can flag partial fills),
audit FKs to the upstream template/clients/accounts/cards, and human-readable
``*_snapshot`` strings so the row remains useful after upstream deletes.
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
from app.routes.entities_htmx import entity_label
from app.schemas.template import TemplateFillRequest
from app.utils.errors import NotFoundError

NAME_MAX_LEN = 255
_ROLES: tuple[str, ...] = ("sender", "receiver", "accountOwner")


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

    async def save_from_fill(
        self,
        *,
        template: MessageTemplate,
        fill_request: TemplateFillRequest,
        rendered: str,
        changed: list[str],
        unresolved: list[str],
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
