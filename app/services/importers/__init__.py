"""Collection importers (Postman v2.1, later Insomnia, …).

Each importer is a pure function turning an external collection document into a
common :class:`~app.services.importers.base.ParsedCollection`, so the
persistence layer (:class:`~app.services.collections.CollectionService`) stays
format-agnostic.
"""

from __future__ import annotations

from app.services.importers.base import ParsedCollection, ParsedRequest
from app.services.importers.postman import parse_postman_collection

__all__ = ["ParsedCollection", "ParsedRequest", "parse_postman_collection"]
