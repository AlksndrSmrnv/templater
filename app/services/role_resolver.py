from __future__ import annotations

import re

ROLE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "sender": (
        "sender",
        "payer",
        "debtor",
        "originator",
        "initiator",
        "remitter",
        "from",
        "source",
        "отправитель",
        "плательщик",
        "дебитор",
        "инициатор",
    ),
    "receiver": (
        "receiver",
        "recipient",
        "payee",
        "creditor",
        "beneficiary",
        "addressee",
        "to",
        "target",
        "получатель",
        "кредитор",
        "бенефициар",
        "адресат",
    ),
    "accountOwner": (
        "accountowner",
        "owner",
        "holder",
        "accountholder",
        "cardholder",
        "владелец",
        "держатель",
    ),
}

_ROLE_PRIORITY = ("accountOwner", "receiver", "sender")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-zа-яё0-9])(?=[A-ZА-ЯЁ])")
_ACRONYM_BOUNDARY = re.compile(r"(?<=[A-ZА-ЯЁ])(?=[A-ZА-ЯЁ][a-zа-яё])")
_NON_TOKEN_CHARS = re.compile(r"[_\W]+", flags=re.UNICODE)
_PATH_INDEX = re.compile(r"\[[^\]]*\]")


def resolve_role_from_path(location: str) -> str | None:
    """Resolve participant role from a JSON-pointer / XML leaf path."""

    for segment in reversed(_path_segments(location)):
        tokens = _segment_tokens(segment)
        if not tokens:
            continue
        roles = {
            role
            for role, synonyms in ROLE_SYNONYMS.items()
            if tokens.intersection(synonyms)
        }
        for role in _ROLE_PRIORITY:
            if role in roles:
                return role
    return None


def _path_segments(location: str) -> list[str]:
    if not location:
        return []
    segments = []
    for raw_segment in location.split("/"):
        segment = raw_segment.replace("~1", "/").replace("~0", "~").strip()
        segment = _PATH_INDEX.sub("", segment)
        if segment.startswith("@"):
            segment = segment[1:]
        if not segment or segment == "#text":
            continue
        segments.append(segment)
    return segments


def _segment_tokens(segment: str) -> set[str]:
    spaced = _CAMEL_BOUNDARY.sub(" ", segment)
    spaced = _ACRONYM_BOUNDARY.sub(" ", spaced)
    spaced = _NON_TOKEN_CHARS.sub(" ", spaced)
    return {token.lower() for token in spaced.split() if token}
