"""Visibility guard shared by the fill flow and the role-switch flows.

A fill (and a later client switch) may name a private client/account/card. The
pickers are already filtered by unlocked groups, but a hand-crafted POST could
reference an entity the caller can't see — without this check its attribute
values would leak into the rendered output. Lives here (not on a route module)
so both ``routes/templates_reg.py`` and the filled-template / chain services can
import it without a routes→routes cycle.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.template import TemplateFillRequest
from app.services.entities import AccountService, CardService, ClientService
from app.utils.errors import ValidationFailed


async def assert_fill_visible(
    session: AsyncSession,
    data: TemplateFillRequest,
    visible_group_ids: set[uuid.UUID] | None,
) -> None:
    """Reject a fill referencing entities the caller cannot see.

    ``visible_group_ids=None`` skips the check (internal callers)."""

    if visible_group_ids is None:
        return
    checks = (
        (ClientService, (data.sender_client_id, data.receiver_client_id, data.account_owner_client_id)),
        (AccountService, (data.sender_account_id, data.receiver_account_id, data.account_owner_account_id)),
        (CardService, (data.sender_card_id, data.receiver_card_id, data.account_owner_card_id)),
    )
    for service_cls, ids in checks:
        wanted = {i for i in ids if i is not None}
        if not wanted:
            continue
        rows = await service_cls(session).get_many(
            list(wanted), visible_group_ids=visible_group_ids
        )
        if wanted - {row.id for row in rows}:
            raise ValidationFailed("Выбраны недоступные записи — разблокируйте нужную группу паролем")
