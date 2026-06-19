from __future__ import annotations

import uuid

from pydantic import BaseModel


class ImportCollectionSummary(BaseModel):
    """Outcome of importing one collection file."""

    collection_id: uuid.UUID
    name: str
    templates_created: int
    unparsable: int = 0

