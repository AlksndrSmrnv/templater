"""Configurable generation patterns for dynamic envelope fields.

The four dynamic tokens (``rqUID``/``operUID``/``rqTm``/``channelDateTime`` —
see :mod:`app.services.dynamic_fields`) are regenerated on **every** send and
substituted into the request body and headers. This module owns the *patterns*
that drive that generation: a per-field template string the user edits on the
settings page. The values themselves are produced client-side at send time
(``status_code.js#generateDynamicValue``); the pattern grammar it understands is
documented on the settings page.

Storage is a single :class:`AppSetting` row (``dynamic_field_patterns``) holding
the ``{token: pattern}`` map. Anything missing/blank falls back to the field's
default, so a send can never emit a bare ``{{token}}``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.settings import SettingsRepository

DYNAMIC_PATTERNS_KEY = "dynamic_field_patterns"

# Canonical token -> default generation pattern. IDs default to a random UUID;
# the datetime fields to a plain ISO-8601 local timestamp (no zone/millis) —
# users tune both on the settings page.
DEFAULT_DYNAMIC_PATTERNS: dict[str, str] = {
    "rqUID": "{uuid}",
    "operUID": "{uuid}",
    "rqTm": "{date:YYYY-MM-DDTHH:mm:ss}",
    "channelDateTime": "{date:YYYY-MM-DDTHH:mm:ss}",
}

# Ordered UI metadata for the settings form. Order matches the dynamic-token
# catalog in :mod:`app.services.dynamic_fields`.
DYNAMIC_PATTERN_FIELDS: tuple[dict[str, str], ...] = (
    {"name": "rqUID", "label": "ID запроса (rqUID)"},
    {"name": "operUID", "label": "ID операции (operUID)"},
    {"name": "rqTm", "label": "Время запроса (rqTm)"},
    {"name": "channelDateTime", "label": "Дата/время канала (channelDateTime)"},
)

# Cap a single pattern so a hand-crafted request can't store an unbounded blob.
_MAX_PATTERN_LEN = 200


def default_dynamic_patterns() -> dict[str, str]:
    """A fresh copy of the built-in defaults (safe for the caller to mutate)."""

    return dict(DEFAULT_DYNAMIC_PATTERNS)


def dynamic_pattern_fields() -> list[dict[str, str]]:
    """UI catalog (name + label) for the settings form, independent copies."""

    return [dict(entry) for entry in DYNAMIC_PATTERN_FIELDS]


def normalize_dynamic_patterns(raw: Any) -> dict[str, str]:
    """Overlay a submitted/stored mapping onto the defaults.

    Only known canonical fields are kept; a blank, oversized, or non-string
    value falls back to that field's default so the result always has a usable
    pattern for every field.
    """

    out = default_dynamic_patterns()
    if isinstance(raw, dict):
        for name in out:
            value = raw.get(name)
            if isinstance(value, str):
                cleaned = value.strip()[:_MAX_PATTERN_LEN]
                if cleaned:
                    out[name] = cleaned
    return out


async def load_dynamic_patterns(session: AsyncSession) -> dict[str, str]:
    """Current patterns (defaults overlaid by the saved settings row)."""

    saved = await SettingsRepository(session).get(DYNAMIC_PATTERNS_KEY)
    return normalize_dynamic_patterns(saved)


async def save_dynamic_patterns(session: AsyncSession, raw: Any) -> dict[str, str]:
    """Persist normalized patterns; returns what was stored. Does not commit."""

    patterns = normalize_dynamic_patterns(raw)
    await SettingsRepository(session).set(DYNAMIC_PATTERNS_KEY, patterns)
    return patterns
