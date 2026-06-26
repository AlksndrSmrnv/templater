"""Shared helpers for the «Заменить клиента» flows on the filled-template panel
and in the chain panel — parsing a role triple out of a switch form.

Lives in its own module (not on a route module) so both
``routes/filled_templates.py`` and ``routes/chains.py`` import it without a
routes→routes cycle, mirroring ``services/fill_access.py``.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.routes.htmx_utils import form_str
from app.services.filled_templates import _ROLE_COLUMNS, _RoleIds

# The roles a switch may target — the single source of truth is the role-column
# map on the service.
SWITCH_ROLES: frozenset[str] = frozenset(_ROLE_COLUMNS)


def role_ids_from_form(form: Any) -> _RoleIds:
    """Parse a ``(client_id, account_id, card_id)`` triple from switch form
    fields. Empty strings become ``None``; a malformed UUID raises ``ValueError``."""

    def _uuid_or_none(key: str) -> uuid.UUID | None:
        raw = form_str(form, key)
        return uuid.UUID(raw) if raw else None

    return _RoleIds(
        client_id=_uuid_or_none("client_id"),
        account_id=_uuid_or_none("account_id"),
        card_id=_uuid_or_none("card_id"),
    )
