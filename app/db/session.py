from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _ensure_engine() -> None:
    global _engine, _sessionmaker
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            future=True,
            # Pin every pooled connection to the dedicated schema. All tables
            # and queries then resolve inside it without schema-qualifying
            # anything in the models.
            connect_args={"server_settings": {"search_path": settings.db_schema}},
        )
        _sessionmaker = async_sessionmaker(
            bind=_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    _ensure_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    factory = get_sessionmaker()
    async with factory() as session:
        yield session


async def shutdown_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None


def get_engine() -> AsyncEngine:
    _ensure_engine()
    assert _engine is not None
    return _engine
