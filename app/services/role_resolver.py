from __future__ import annotations

from app.utils.paths import pointer_path_segments, segment_tokens

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


def resolve_role_from_path(location: str) -> str | None:
    """Resolve participant role from a JSON-pointer / XML leaf path."""

    for segment in reversed(pointer_path_segments(location)):
        tokens = segment_tokens(segment)
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
