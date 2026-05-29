from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CollectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str = ""
    source: str = "postman"
    source_format: str = ""
    created_at: datetime
    updated_at: datetime


class ImportCollectionSummary(BaseModel):
    """Outcome of importing one collection file."""

    collection_id: uuid.UUID
    name: str
    templates_created: int
    unparsable: int = 0


class ProcessCollectionSummary(BaseModel):
    """Outcome of running LLM analysis across a collection's templates."""

    processed: int = 0
    skipped: int = 0
    failed: int = 0
