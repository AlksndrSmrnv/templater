"""Collection importers (Postman v2.1, Insomnia v4, …).

Each importer is a pure function turning an external collection document into a
common :class:`~app.services.importers.base.ParsedCollection`, so the
persistence layer (:class:`~app.services.collections.CollectionService`) stays
format-agnostic. :func:`detect_and_parse` sniffs the format and dispatches.
"""

from __future__ import annotations

from typing import Any

from app.services.importers.base import ParsedCollection, ParsedRequest
from app.services.importers.insomnia import parse_insomnia_collection
from app.services.importers.postman import parse_postman_collection
from app.utils.errors import ValidationFailed

__all__ = [
    "ParsedCollection",
    "ParsedRequest",
    "detect_and_parse",
    "parse_insomnia_collection",
    "parse_postman_collection",
]


def detect_and_parse(data: Any) -> ParsedCollection:
    """Sniff the collection format from its top-level shape and parse it.

    An Insomnia v4 export is a flat ``resources`` list; a Postman collection
    carries ``info`` + ``item``. Anything else is rejected.
    """

    if isinstance(data, dict):
        if isinstance(data.get("resources"), list):
            return parse_insomnia_collection(data)
        if "info" in data or "item" in data:
            return parse_postman_collection(data)
    raise ValidationFailed(
        "Не удалось определить формат коллекции (поддерживаются Postman v2.1 и Insomnia v4)"
    )
