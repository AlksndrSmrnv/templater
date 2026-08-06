from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.services.transfer_assistant import (
    TransferAssistant,
    TransferConstraints,
    _ambiguous_requested_surname,
    _client_full_name,
    _extract_transfer_constraints,
    _template_matches,
)
from app.utils.errors import ValidationFailed


def _client(**attrs: Any) -> Any:
    return SimpleNamespace(id=uuid.uuid4(), attributes=attrs, description="", tags=[])


def _account(client_id: Any, **attrs: Any) -> Any:
    return SimpleNamespace(id=uuid.uuid4(), client_id=client_id, attributes=attrs,
                           description="", tags=[])


def _card(account_id: Any) -> Any:
    return SimpleNamespace(id=uuid.uuid4(), account_id=account_id, attributes={},
                           description="", tags=[])


def _assistant() -> TransferAssistant:
    # Pure helper methods don't touch the session; a bare instance is enough.
    return TransferAssistant.__new__(TransferAssistant)


def _template(summary: str, *, name: str = "Шаблон", description: str = "Описание") -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        format="json",
        content="{}",
        original_content="{}",
        description=description,
        llm_meta={
            "import_status": "processed",
            "summary": summary,
            "has_account_owner": False,
        },
        placeholders=[{"location": "/a", "mode": "literal", "value": "x"}],
    )


class _TemplateList:
    def __init__(self, items: list[Any]) -> None:
        self.items = items

    async def list_all(self) -> list[Any]:
        return self.items


class _EntityList:
    def __init__(self, items: list[Any] | None = None) -> None:
        self.items = items or []

    async def list_all(self, *, visible_group_ids: Any = None) -> list[Any]:
        return self.items


class _CapturingLLM:
    def __init__(self, choice: str) -> None:
        self.choice = choice
        self.template_catalogs: list[list[dict[str, str]]] = []
        self.participant_clients: list[list[dict[str, str]]] = []
        self.participants_called = False

    async def pick_transfer_template(
        self, *, request: str, templates: list[dict[str, str]]
    ) -> tuple[str, dict[str, str]]:
        self.template_catalogs.append(templates)
        return self.choice, {}

    async def pick_transfer_participants(
        self, *, clients: list[dict[str, str]], **_: Any
    ) -> tuple[dict[str, Any], dict[str, str]]:
        self.participants_called = True
        self.participant_clients.append(clients)
        return {}, {}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("TDD", "A2A"),
        ("A2A", "A2A"),
        ("перевод со счёта на счёт", "A2A"),
        ("TDC", "A2C"),
        ("A2C", "A2C"),
        ("перевод со счета на карту", "A2C"),
        ("перевод со счёта в долларах на карту", "A2C"),
        ("TCD", "C2A"),
        ("C2A", "C2A"),
        ("перевод с карты на счёт", "C2A"),
        ("перевод с карты в долларах на счёт в рублях", "C2A"),
        ("TCC", "C2C"),
        ("C2C", "C2C"),
        ("перевод с карты на карту", "C2C"),
    ],
)
def test_extract_transfer_constraints_recognizes_instrument_type(
    text: str, expected: str
) -> None:
    assert _extract_transfer_constraints(text).instruments == frozenset({expected})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("перевод самому себе", "self"),
        ("плательщик и получатель совпадают", "self"),
        ("перевод между своими счетами", "self"),
        ("another_int", "internal"),
        ("другому клиенту этого же банка", "internal"),
        ("перевод внутри банка", "internal"),
        ("another_ext", "external"),
        ("клиенту в другой банк", "external"),
        ("перевод в сторонний банк", "external"),
    ],
)
def test_extract_transfer_constraints_recognizes_recipient_type(
    text: str, expected: str
) -> None:
    assert _extract_transfer_constraints(text).recipients == frozenset({expected})


def test_extract_transfer_constraints_preserves_conflicts() -> None:
    constraints = _extract_transfer_constraints("TDD и TCC, самому себе в другой банк")

    assert constraints.instruments == frozenset({"A2A", "C2C"})
    assert constraints.recipients == frozenset({"self", "external"})


@pytest.mark.parametrize(
    "text",
    [
        "баланс карты на счет",
        "прогресс счета на карту",
    ],
)
def test_extract_transfer_constraints_does_not_match_suffix_of_previous_word(
    text: str,
) -> None:
    assert _extract_transfer_constraints(text).instruments == frozenset()


def test_extract_transfer_constraints_does_not_assume_between_accounts_is_self() -> None:
    constraints = _extract_transfer_constraints("перевод между счетами Иванова и Петрова")

    assert constraints.instruments == frozenset({"A2A"})
    assert constraints.recipients == frozenset()


def test_extract_transfer_constraints_recognizes_colloquial_self() -> None:
    assert _extract_transfer_constraints("перевод сам себе").recipients == frozenset(
        {"self"}
    )


def test_extract_transfer_constraints_recognizes_feminine_colloquial_self() -> None:
    assert _extract_transfer_constraints("перевод сама себе").recipients == frozenset(
        {"self"}
    )


def test_extract_transfer_constraints_does_not_bridge_enumerated_types() -> None:
    constraints = _extract_transfer_constraints(
        "Переводы со счёта на счёт и с карты на карту"
    )

    assert constraints.instruments == frozenset({"A2A", "C2C"})


def test_extract_transfer_constraints_ignores_negated_recipient() -> None:
    constraints = _extract_transfer_constraints("не для переводов в другой банк")

    assert constraints.recipients == frozenset()


def test_extract_transfer_constraints_does_not_apply_distant_exception() -> None:
    constraints = _extract_transfer_constraints(
        "Переведи всё кроме суммы со счёта на карту"
    )

    assert constraints.instruments == frozenset({"A2C"})


def test_extract_transfer_constraints_ignores_directly_negated_instrument() -> None:
    constraints = _extract_transfer_constraints(
        "подбери всё кроме переводов со счёта на карту"
    )

    assert constraints.instruments == frozenset()


def test_template_matches_known_value_and_keeps_unknown_or_multi_value_candidate() -> None:
    requested = TransferConstraints(instruments=frozenset({"A2A"}))

    assert _template_matches(requested, TransferConstraints()) is True
    assert (
        _template_matches(
            requested,
            TransferConstraints(instruments=frozenset({"A2A", "C2C"})),
        )
        is True
    )
    assert (
        _template_matches(
            requested,
            TransferConstraints(instruments=frozenset({"C2C"})),
        )
        is False
    )


@pytest.mark.parametrize(
    ("attrs", "expected"),
    [
        ({"fullName": "Иванов Иван"}, "Иванов Иван"),
        ({"name": "ООО Ромашка"}, "ООО Ромашка"),
        ({"shortName": "Ромашка"}, "Ромашка"),
        ({}, ""),
    ],
)
def test_client_full_name_uses_supported_fallbacks(
    attrs: dict[str, str], expected: str
) -> None:
    assert _client_full_name(attrs) == expected


def test_ambiguous_surname_accepts_unique_full_name() -> None:
    clients = [
        _client(fullName="Иванов Иван Иванович"),
        _client(fullName="Иванов Пётр Петрович"),
    ]

    assert (
        _ambiguous_requested_surname(
            "перевод от клиента Иванов Иван Иванович", clients
        )
        is None
    )


def test_ambiguous_surname_accepts_declined_unique_full_name() -> None:
    clients = [
        _client(fullName="Иванов Иван Иванович"),
        _client(fullName="Иванов Пётр Петрович"),
    ]

    assert (
        _ambiguous_requested_surname(
            "перевод от клиента Иванова Ивана Ивановича", clients
        )
        is None
    )


def test_ambiguous_surname_rejects_duplicate_full_name() -> None:
    clients = [
        _client(fullName="Иванов Иван Иванович"),
        _client(fullName="Иванов Иван Иванович"),
    ]

    assert (
        _ambiguous_requested_surname(
            "перевод от клиента Иванов Иван Иванович", clients
        )
        == "Иванов"
    )


def test_ambiguous_surname_recognizes_common_russian_case() -> None:
    clients = [
        _client(fullName="Иванов Иван Иванович"),
        _client(fullName="Иванов Пётр Петрович"),
    ]

    assert _ambiguous_requested_surname("перевод от клиента Иванова", clients) == "Иванов"


def test_ambiguous_surname_skips_legal_entity_prefix() -> None:
    clients = [
        _client(fullName="ООО Ромашка"),
        _client(fullName="ООО Василёк"),
    ]

    assert _ambiguous_requested_surname("перевод для ООО", clients) is None


@pytest.mark.asyncio
async def test_compose_offers_only_templates_matching_explicit_constraints() -> None:
    a2a_self = _template("TDD/A2A — со счёта на счёт, самому себе")
    c2c_external = _template("TCC/C2C — с карты на карту, another_ext")
    assistant = _assistant()
    assistant.templates = _TemplateList([a2a_self, c2c_external])  # type: ignore[assignment]
    assistant.clients = _EntityList()  # type: ignore[assignment]
    assistant.accounts = _EntityList()  # type: ignore[assignment]
    assistant.cards = _EntityList()  # type: ignore[assignment]
    llm = _CapturingLLM("T2")

    with pytest.raises(ValidationFailed, match="участников"):
        await assistant.compose("TCC клиенту в другой банк", llm)

    assert [row["id"] for row in llm.template_catalogs[0]] == ["T2"]


@pytest.mark.asyncio
async def test_compose_keeps_template_with_unknown_constraints_for_llm_ranking() -> None:
    assistant = _assistant()
    assistant.templates = _TemplateList([_template("Перевод без технических кодов")])  # type: ignore[assignment]
    assistant.clients = _EntityList()  # type: ignore[assignment]
    assistant.accounts = _EntityList()  # type: ignore[assignment]
    assistant.cards = _EntityList()  # type: ignore[assignment]
    llm = _CapturingLLM("T1")

    with pytest.raises(ValidationFailed, match="участников"):
        await assistant.compose("TCC клиенту в другой банк", llm)

    assert [row["id"] for row in llm.template_catalogs[0]] == ["T1"]


@pytest.mark.asyncio
async def test_compose_normalizes_none_template_summary() -> None:
    template = _template("unused")
    template.llm_meta["summary"] = None
    assistant = _assistant()
    assistant.templates = _TemplateList([template])  # type: ignore[assignment]
    assistant.clients = _EntityList()  # type: ignore[assignment]
    assistant.accounts = _EntityList()  # type: ignore[assignment]
    assistant.cards = _EntityList()  # type: ignore[assignment]
    llm = _CapturingLLM("T1")

    with pytest.raises(ValidationFailed, match="участников"):
        await assistant.compose("обычный перевод", llm)

    assert llm.template_catalogs[0][0]["summary"] == ""


@pytest.mark.asyncio
async def test_compose_rejects_llm_id_excluded_by_explicit_constraints() -> None:
    a2a_self = _template("TDD/A2A — со счёта на счёт, самому себе")
    c2c_external = _template("TCC/C2C — с карты на карту, another_ext")
    assistant = _assistant()
    assistant.templates = _TemplateList([a2a_self, c2c_external])  # type: ignore[assignment]
    assistant.clients = _EntityList()  # type: ignore[assignment]
    assistant.accounts = _EntityList()  # type: ignore[assignment]
    assistant.cards = _EntityList()  # type: ignore[assignment]
    llm = _CapturingLLM("T1")

    with pytest.raises(ValidationFailed, match="подходящий шаблон"):
        await assistant.compose("TCC клиенту в другой банк", llm)

    assert llm.participants_called is False


@pytest.mark.asyncio
async def test_compose_rejects_explicit_type_without_exact_template() -> None:
    assistant = _assistant()
    assistant.templates = _TemplateList(
        [_template("TDD/A2A — со счёта на счёт, самому себе")]
    )  # type: ignore[assignment]
    llm = _CapturingLLM("T1")

    with pytest.raises(ValidationFailed, match="не противоречащий"):
        await assistant.compose("TCC клиенту в другой банк", llm)

    assert llm.template_catalogs == []


@pytest.mark.asyncio
async def test_compose_rejects_conflicting_explicit_types() -> None:
    assistant = _assistant()
    assistant.templates = _TemplateList(
        [_template("TDD/A2A — со счёта на счёт, самому себе")]
    )  # type: ignore[assignment]
    llm = _CapturingLLM("T1")

    with pytest.raises(ValidationFailed, match="противоречащие типы"):
        await assistant.compose("TDD и TCC", llm)

    assert llm.template_catalogs == []


@pytest.mark.asyncio
async def test_compose_passes_client_full_name_to_participant_catalog() -> None:
    assistant = _assistant()
    assistant.templates = _TemplateList([_template("Обычный перевод")])  # type: ignore[assignment]
    assistant.clients = _EntityList([_client(fullName="Иванов Иван Иванович")])  # type: ignore[assignment]
    assistant.accounts = _EntityList()  # type: ignore[assignment]
    assistant.cards = _EntityList()  # type: ignore[assignment]
    llm = _CapturingLLM("T1")

    with pytest.raises(ValidationFailed, match="участников"):
        await assistant.compose("перевод от клиента Иванов", llm)

    assert llm.participant_clients[0][0]["full_name"] == "Иванов Иван Иванович"


@pytest.mark.asyncio
async def test_compose_rejects_ambiguous_requested_surname() -> None:
    assistant = _assistant()
    assistant.templates = _TemplateList([_template("Обычный перевод")])  # type: ignore[assignment]
    assistant.clients = _EntityList(
        [
            _client(fullName="Иванов Иван Иванович"),
            _client(fullName="Иванов Пётр Петрович"),
        ]
    )  # type: ignore[assignment]
    assistant.accounts = _EntityList()  # type: ignore[assignment]
    assistant.cards = _EntityList()  # type: ignore[assignment]
    llm = _CapturingLLM("T1")

    with pytest.raises(ValidationFailed, match="несколько клиентов с фамилией Иванов"):
        await assistant.compose("перевод от клиента Иванова", llm)

    assert llm.template_catalogs == []
    assert llm.participants_called is False


@pytest.mark.asyncio
async def test_compose_accepts_full_name_among_same_surname_clients() -> None:
    assistant = _assistant()
    assistant.templates = _TemplateList([_template("Обычный перевод")])  # type: ignore[assignment]
    assistant.clients = _EntityList(
        [
            _client(fullName="Иванов Иван Иванович"),
            _client(fullName="Иванов Пётр Петрович"),
        ]
    )  # type: ignore[assignment]
    assistant.accounts = _EntityList()  # type: ignore[assignment]
    assistant.cards = _EntityList()  # type: ignore[assignment]
    llm = _CapturingLLM("T1")

    with pytest.raises(ValidationFailed, match="участников"):
        await assistant.compose("перевод от клиента Иванова Ивана Ивановича", llm)

    assert [row["id"] for row in llm.template_catalogs[0]] == ["T1"]
    assert len(llm.participant_clients[0]) == 2


@pytest.mark.asyncio
async def test_compose_does_not_treat_hidden_same_surname_as_ambiguous() -> None:
    """EntityService already applies the visible-group filter; ambiguity must be
    computed only from the rows it returned, never from hidden clients."""

    assistant = _assistant()
    assistant.templates = _TemplateList([_template("Обычный перевод")])  # type: ignore[assignment]
    assistant.clients = _EntityList([_client(fullName="Иванов Иван Иванович")])  # type: ignore[assignment]
    assistant.accounts = _EntityList()  # type: ignore[assignment]
    assistant.cards = _EntityList()  # type: ignore[assignment]
    llm = _CapturingLLM("T1")

    with pytest.raises(ValidationFailed, match="участников"):
        await assistant.compose(
            "перевод от клиента Иванов",
            llm,
            visible_group_ids={uuid.uuid4()},
        )

    assert llm.participants_called is True


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
        name="Visible groups",
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
        async def pick_transfer_template(
            self, *, request: str, templates: Any
        ) -> tuple[str, dict[str, Any]]:
            return "T1", {}

        async def pick_transfer_participants(self, **_: Any) -> tuple[dict[str, Any], dict[str, Any]]:
            return {}, {}

    a = TransferAssistant.__new__(TransferAssistant)
    a.session = cast(Any, object())  # not reached: no participants resolve → raises first
    a.templates = _FakeTemplates()  # type: ignore[assignment]
    a.clients = _SpyEntityService("clients")  # type: ignore[assignment]
    a.accounts = _SpyEntityService("accounts")  # type: ignore[assignment]
    a.cards = _SpyEntityService("cards")  # type: ignore[assignment]

    groups = {uuid.uuid4()}
    with pytest.raises(ValidationFailed):  # empty picks → can't resolve participants
        await a.compose("перевод", _FakeLLM(), visible_group_ids=groups)

    # All three participant catalogs were fetched with the caller's group set.
    assert captured == {"clients": groups, "accounts": groups, "cards": groups}
