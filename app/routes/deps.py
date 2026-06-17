from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_sessionmaker
from app.utils.access_groups import unlocked_group_ids

_jinja: Jinja2Templates | None = None


def get_templates() -> Jinja2Templates:
    global _jinja
    if _jinja is None:
        settings = get_settings()
        _jinja = Jinja2Templates(directory=str(settings.templates_dir))
        _jinja.env.globals["llm_active"] = settings.llm_active
    return _jinja


async def db_session() -> AsyncIterator[AsyncSession]:
    factory = get_sessionmaker()
    async with factory() as session:
        yield session


def current_group_ids(request: Request) -> set[uuid.UUID]:
    """Access groups the current request has unlocked (empty when none).

    Public rows are always visible regardless; routes pass this set down to the
    services/repositories so test data and filled templates outside it stay
    hidden.
    """

    return unlocked_group_ids(request)


SessionDep = Depends(db_session)
TemplatesDep = Depends(get_templates)
UnlockedGroupsDep = Depends(current_group_ids)
