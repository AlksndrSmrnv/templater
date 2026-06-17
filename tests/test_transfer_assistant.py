from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.transfer_assistant import TransferAssistant
from app.utils.errors import ValidationFailed


def _client(**attrs):
    return SimpleNamespace(id=uuid.uuid4(), attributes=attrs, description="", tags=[])


def _account(client_id, **attrs):
    return SimpleNamespace(id=uuid.uuid4(), client_id=client_id, attributes=attrs,
                           description="", tags=[])


def _card(account_id):
    return SimpleNamespace(id=uuid.uuid4(), account_id=account_id, attributes={},
                           description="", tags=[])


def _assistant() -> TransferAssistant:
    # Pure helper methods don't touch the session; a bare instance is enough.
    return TransferAssistant.__new__(TransferAssistant)


def test_resolve_picks_keeps_consistent_account_and_card() -> None:
    c1 = _client(residency="resident")
    a1 = _account(c1.id, currency_id="USD")
    k1 = _card(a1.id)
    resolved = _assistant()._resolve_picks(
        {"sender": {"client": "C1", "account": "A1", "card": "K1"}},
        client_ids={"C1": c1},
        account_ids={"A1": a1},
        card_ids={"K1": k1},
        allow_account_owner=False,
    )
    assert resolved["sender"]["client"] is c1
    assert resolved["sender"]["account"] is a1
    assert resolved["sender"]["card"] is k1


def test_resolve_picks_drops_account_not_owned_by_client() -> None:
    c1, c2 = _client(), _client()
    a_other = _account(c2.id)  # belongs to a different client
    resolved = _assistant()._resolve_picks(
        {"sender": {"client": "C1", "account": "A1", "card": None}},
        client_ids={"C1": c1},
        account_ids={"A1": a_other},
        card_ids={},
        allow_account_owner=False,
    )
    # Mismatched account is dropped → filler falls back to client's own account.
    assert resolved["sender"]["account"] is None


def test_resolve_picks_drops_card_not_matching_chosen_account() -> None:
    c1 = _client()
    a1 = _account(c1.id)
    a2 = _account(c1.id)
    card_of_a2 = _card(a2.id)
    resolved = _assistant()._resolve_picks(
        {"sender": {"client": "C1", "account": "A1", "card": "K1"}},
        client_ids={"C1": c1},
        account_ids={"A1": a1, "A2": a2},
        card_ids={"K1": card_of_a2},
        allow_account_owner=False,
    )
    assert resolved["sender"]["account"] is a1
    assert resolved["sender"]["card"] is None  # card belongs to A2, not chosen A1


def test_resolve_picks_skips_account_owner_when_not_allowed() -> None:
    c1, c2 = _client(), _client()
    resolved = _assistant()._resolve_picks(
        {
            "sender": {"client": "C1"},
            "accountOwner": {"client": "C2"},
        },
        client_ids={"C1": c1, "C2": c2},
        account_ids={},
        card_ids={},
        allow_account_owner=False,
    )
    assert "accountOwner" not in resolved
    assert "sender" in resolved


def test_fill_kwargs_maps_roles_to_fill_keys() -> None:
    c1 = _client()
    a1 = _account(c1.id)
    resolved = {"sender": {"client": c1, "account": a1, "card": None}}
    kwargs = TransferAssistant._fill_kwargs(resolved)
    assert kwargs["sender_client_id"] == c1.id
    assert kwargs["sender_account_id"] == a1.id
    assert kwargs["sender_card_id"] is None
    # Untouched roles default to None across all nine keys.
    assert kwargs["receiver_client_id"] is None
    assert kwargs["account_owner_client_id"] is None


def test_merge_debug_labels_both_llm_calls() -> None:
    template = {"system_prompt": "ts", "user_prompt": "tu", "response_text": "tr"}
    participants = {"system_prompt": "ps", "user_prompt": "pu", "response_text": "pr"}
    merged = TransferAssistant._merge_debug(template=template, participants=participants)
    assert set(merged) == {"system_prompt", "user_prompt", "response_text"}
    # Each key carries both calls under their labelled sections.
    assert merged["system_prompt"] == "### Подбор шаблона\nts\n\n### Подбор участников\nps"
    assert "### Подбор шаблона\ntr" in merged["response_text"]
    assert "### Подбор участников\npr" in merged["response_text"]


@pytest.mark.asyncio
async def test_compose_filters_participants_by_visible_groups() -> None:
    """The participant catalogs the LLM sees must be restricted to the caller's
    unlocked groups — otherwise hidden test data would be sent to the model and
    rendered back to someone who never unlocked it."""

    captured: dict[str, Any] = {}
    tpl = SimpleNamespace(
        id=uuid.uuid4(),
        format="json",
        content="{}",
        description="d",
        # has_account_owner short-circuits template_has_account_owner before it
        # inspects the (faked) body — this test only cares about group filtering.
        llm_meta={"import_status": "processed", "summary": "s", "has_account_owner": True},
        placeholders=[{"location": "/a", "mode": "literal", "value": "x"}],
    )

    class _FakeTemplates:
        async def list_all(self) -> list[Any]:
            return [tpl]

    class _SpyEntityService:
        def __init__(self, key: str) -> None:
            self.key = key

        async def list_all(self, *, visible_group_ids: Any = None) -> list[Any]:
            captured[self.key] = visible_group_ids
            return []

    class _FakeLLM:
        async def pick_transfer_template(self, *, request: str, templates: Any) -> tuple[str, dict]:
            return "T1", {}

        async def pick_transfer_participants(self, **_: Any) -> tuple[dict, dict]:
            return {}, {}

    a = TransferAssistant.__new__(TransferAssistant)
    a.session = object()  # not reached: no participants resolve → raises first
    a.templates = _FakeTemplates()  # type: ignore[assignment]
    a.clients = _SpyEntityService("clients")  # type: ignore[assignment]
    a.accounts = _SpyEntityService("accounts")  # type: ignore[assignment]
    a.cards = _SpyEntityService("cards")  # type: ignore[assignment]

    groups = {uuid.uuid4()}
    with pytest.raises(ValidationFailed):  # empty picks → can't resolve participants
        await a.compose("перевод", _FakeLLM(), visible_group_ids=groups)

    # All three participant catalogs were fetched with the caller's group set.
    assert captured == {"clients": groups, "accounts": groups, "cards": groups}
