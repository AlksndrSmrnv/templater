from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_sessionmaker

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


SessionDep = Depends(db_session)
TemplatesDep = Depends(get_templates)
