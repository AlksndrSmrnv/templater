"""Idempotently seed default reference values (currencies).

Run via `python -m scripts.seed_reference_data`. Existing rows (matched by
entity_type + code) are left untouched.
"""
from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ReferenceValue
from app.db.session import get_sessionmaker, shutdown_engine

SEED: dict[str, list[dict[str, Any]]] = {
    "currency": [
        {"code": "RUB", "name": "Российский рубль", "attributes": {"iso_code": "RUB", "numeric_code": "643", "symbol": "₽"}},
        {"code": "USD", "name": "Доллар США", "attributes": {"iso_code": "USD", "numeric_code": "840", "symbol": "$"}},
        {"code": "EUR", "name": "Евро", "attributes": {"iso_code": "EUR", "numeric_code": "978", "symbol": "€"}},
        {"code": "GBP", "name": "Фунт стерлингов", "attributes": {"iso_code": "GBP", "numeric_code": "826", "symbol": "£"}},
        {"code": "CNY", "name": "Китайский юань", "attributes": {"iso_code": "CNY", "numeric_code": "156", "symbol": "¥"}},
        {"code": "KZT", "name": "Казахстанский тенге", "attributes": {"iso_code": "KZT", "numeric_code": "398", "symbol": "₸"}},
        {"code": "BYN", "name": "Белорусский рубль", "attributes": {"iso_code": "BYN", "numeric_code": "933", "symbol": "Br"}},
        {"code": "CHF", "name": "Швейцарский франк", "attributes": {"iso_code": "CHF", "numeric_code": "756", "symbol": "Fr"}},
        {"code": "JPY", "name": "Японская иена", "attributes": {"iso_code": "JPY", "numeric_code": "392", "symbol": "¥"}},
        {"code": "TRY", "name": "Турецкая лира", "attributes": {"iso_code": "TRY", "numeric_code": "949", "symbol": "₺"}},
    ],
}


async def _seed(session: AsyncSession) -> int:
    inserted = 0
    for entity_type, items in SEED.items():
        existing = {
            code for (code,) in (
                await session.execute(
                    select(ReferenceValue.code).where(ReferenceValue.entity_type == entity_type)
                )
            ).all()
        }
        for item in items:
            if item["code"] in existing:
                continue
            session.add(
                ReferenceValue(
                    entity_type=entity_type,
                    code=item["code"],
                    name=item["name"],
                    description=item.get("description", ""),
                    attributes=item.get("attributes", {}),
                )
            )
            inserted += 1
    await session.commit()
    return inserted


async def main() -> None:
    factory = get_sessionmaker()
    try:
        async with factory() as session:
            inserted = await _seed(session)
            print(f"Seeded {inserted} reference rows")
    finally:
        await shutdown_engine()


if __name__ == "__main__":
    asyncio.run(main())
