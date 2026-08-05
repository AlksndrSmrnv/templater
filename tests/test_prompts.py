from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.repositories.settings as settings_repo_mod
from app.llm.prompts import (
    PROMPT_DEFS,
    PromptBuilder,
    load_prompt_overrides,
    validate_prompt_text,
)
from app.utils.errors import ValidationFailed


def _mapping(
    leaves: list[dict[str, str]], catalog: list[dict[str, str]]
) -> tuple[str, str, dict[str, str]]:
    return PromptBuilder().build_template_field_mapping(leaves=leaves, catalog=catalog)


def test_build_template_field_mapping_numbers_leaves_and_drops_json_blob() -> None:
    sys_p, user_p, id_map = _mapping(
        leaves=[
            {"location": "/Payer/FullName", "value": "Иванов Иван"},
            {"location": "/Payer/INN", "value": "7701234567"},
        ],
        catalog=[{"path": "sender.fullName", "label": "Sender — ФИО", "data_type": "string"}],
    )

    assert id_map == {"L1": "/Payer/FullName", "L2": "/Payer/INN"}
    assert "L1  /Payer/FullName = Иванов Иван" in user_p
    assert "L2  /Payer/INN = 7701234567" in user_p
    assert "sender.fullName — Sender — ФИО" in user_p
    # No JSON blob in the user prompt anymore — plain text only.
    assert "{" not in user_p and "}" not in user_p
    assert "data_type" not in user_p
    # The full template is no longer shipped — only leaf values.
    assert "template" not in user_p


def test_build_template_field_mapping_defines_roles_and_account_owner_rule() -> None:
    sys_p, _, _ = _mapping(
        leaves=[{"location": "/ownerName", "value": "X"}],
        catalog=[{"path": "accountOwner.ownerName", "label": "Owner", "data_type": "string"}],
    )

    assert "sender.* — отправитель" in sys_p
    assert "receiver.* — получатель" in sys_p
    assert "accountOwner.* — третья сторона" in sys_p
    assert "ownerName" in sys_p
    assert "Не сворачивай его в sender или receiver" in sys_p


def test_build_template_field_mapping_tells_model_to_skip_operator() -> None:
    sys_p, _, _ = _mapping(
        leaves=[{"location": "/operator/firstName", "value": "Иван"}],
        catalog=[{"path": "sender.firstName", "label": "Sender — Имя", "data_type": "string"}],
    )

    # The operator is bank staff, not a transfer participant — its fields must
    # stay untouched and never be folded into sender/receiver/accountOwner.
    assert "operator" in sys_p.lower()
    assert "сотрудник банка" in sys_p
    assert "НЕ размечай" in sys_p


def test_build_template_field_mapping_has_few_shot_example() -> None:
    sys_p, _, _ = _mapping(
        leaves=[{"location": "/Payer/INN", "value": "7701234567"}],
        catalog=[{"path": "sender.inn", "label": "Sender — ИНН", "data_type": "string"}],
    )

    assert "Пример." in sys_p
    assert '{"placeholders":[{"leaf":"L1","field":"sender.inn"}]}' in sys_p
    # Demonstrates skipping a leaf that has no matching field.
    assert "пропущен" in sys_p


def test_build_template_field_mapping_explains_pay_tools() -> None:
    sys_p, _, _ = _mapping(
        leaves=[{"location": "/srcPayTool/cardAcctId/cardNumber", "value": "5536"}],
        catalog=[{"path": "sender.card.number", "label": "Sender — Номер", "data_type": "string"}],
    )

    # src = списание = отправитель; dst = зачисление = получатель.
    assert "srcPayTool" in sys_p
    assert "dstPayTool" in sys_p
    assert "ОТПРАВИТЕЛЯ" in sys_p and "sender.*" in sys_p
    assert "ПОЛУЧАТЕЛЯ" in sys_p and "receiver.*" in sys_p
    # Discriminator: depAcctId -> account, cardAcctId -> card.
    assert "depAcctId" in sys_p and "cardAcctId" in sys_p
    assert ".account." in sys_p and ".card." in sys_p
    # Map every informative field, not only the number.
    assert "ВСЕ содержательные поля" in sys_p
    assert "не только номер" in sys_p


def test_build_template_field_mapping_always_maps_client_id() -> None:
    sys_p, _, _ = _mapping(
        leaves=[{"location": "/recipient/clientId", "value": "1002345"}],
        catalog=[
            {"path": "receiver.clientId", "label": "Receiver — id", "data_type": "string"}
        ],
    )

    # clientId must never be skipped — it has a dedicated always-map rule.
    assert "clientId" in sys_p
    assert "sender.clientId" in sys_p and "receiver.clientId" in sys_p
    assert "ВСЕГДА" in sys_p
    assert "никогда не" in sys_p and "пропускай" in sys_p
    # recipient/payee are the receiver role.
    assert "recipient" in sys_p and "payee" in sys_p


def test_build_template_field_mapping_specifies_strict_response_shape() -> None:
    sys_p, _, _ = _mapping(
        leaves=[{"location": "/fullName", "value": "X"}],
        catalog=[],
    )

    assert '{"placeholders":[{"leaf":str,"field":str}]}' in sys_p
    assert "пропусти лист" in sys_p


def test_build_template_field_mapping_drops_legacy_clutter() -> None:
    # The byte-exact warning, envelope-token paragraph and meta instructions are
    # gone — leaf ids and a separate meta call replace them.
    sys_p, _, _ = _mapping(
        leaves=[{"location": "/fullName", "value": "X"}],
        catalog=[],
    )

    assert "побайтово" not in sys_p
    assert "channelDateTime" not in sys_p
    assert "summary" not in sys_p
    assert "productId" not in sys_p


def test_build_template_field_mapping_flattens_and_truncates_values() -> None:
    long_value = "a" * 200
    _, user_p, _ = _mapping(
        leaves=[
            {"location": "/note", "value": "строка1\nстрока2"},
            {"location": "/blob", "value": long_value},
        ],
        catalog=[],
    )

    assert "L1  /note = строка1 строка2" in user_p
    assert "a" * 120 + "…" in user_p
    assert "a" * 121 not in user_p


def test_build_template_field_mapping_preserves_xml_leaf_path_syntax() -> None:
    _, user_p, id_map = _mapping(
        leaves=[
            {"location": "/Root/Item[0]/@id", "value": "1"},
            {"location": "/Root/Item[0]/Name[0]/#text", "value": "X"},
        ],
        catalog=[],
    )

    assert id_map["L1"] == "/Root/Item[0]/@id"
    assert id_map["L2"] == "/Root/Item[0]/Name[0]/#text"
    assert "L1  /Root/Item[0]/@id = 1" in user_p
    assert "L2  /Root/Item[0]/Name[0]/#text = X" in user_p


def test_build_template_meta_returns_strings_without_json_wrapper() -> None:
    sys_p, user_p = PromptBuilder().build_template_meta(content="<a/>", fmt="xml")

    assert isinstance(sys_p, str) and sys_p
    # Transfer-type codes and the cross-/intra-bank cases are still described.
    assert "A2A" in sys_p
    assert "another_ext" in sys_p
    # New required aspects of the summary.
    assert "канал" in sys_p
    assert "подразделени" in sys_p
    assert "владел" in sys_p  # account owner mention
    # Summary-only schema; category/scenarios are gone.
    assert '{"summary": str}' in sys_p
    assert "category" not in sys_p
    assert "scenarios" not in sys_p
    assert user_p.startswith("Формат: xml")
    assert "<a/>" in user_p
    # Plain text, not a json.dumps payload wrapper.
    assert '"template"' not in user_p and '"format"' not in user_p


def test_build_template_meta_compacts_json_and_removes_null_object_keys() -> None:
    _, user_p = PromptBuilder().build_template_meta(
        content=(
            '{\r\n'
            '\t"fullName": "X",\r\n'
            '\t"empty": null,\r\n'
            '\t"nested": {"dropMe": null, "keepMe": "Y"}\r\n'
            '}'
        ),
        fmt="json",
    )

    assert '{"fullName":"X","nested":{"keepMe":"Y"}}' in user_p
    assert "dropMe" not in user_p
    assert "\t" not in user_p


# --- Transfer assistant prompts ---


def test_build_transfer_template_pick_lists_ids_and_asks_for_json() -> None:
    sys_p, user_p = PromptBuilder().build_transfer_template_pick(
        request="перевод с карты в долларах на счёт в рублях",
        templates=[
            {
                "id": "T1",
                "name": "A2A transfer",
                "description": "Ручное описание",
                "summary": "Перевод со счёта на счёт",
            },
            {
                "id": "T2",
                "name": "External transfer",
                "description": "Перевод клиенту стороннего банка",
                "summary": "Перевод в другой банк",
            },
        ],
    )
    assert '{"template"' in sys_p
    assert "перевод с карты" in user_p
    assert "T1 | A2A transfer | Ручное описание | Перевод со счёта на счёт" in user_p
    assert "T2" in user_p
    assert "Явно указанные" in user_p


def test_build_transfer_template_pick_keeps_full_template_text() -> None:
    long_description = "начало " + "д" * 240 + " важный тип TCC в конце"
    long_summary = "резюме " + "с" * 240 + " перевод с карты на карту"

    _, user_p = PromptBuilder().build_transfer_template_pick(
        request="TCC другому клиенту",
        templates=[
            {
                "id": "T1",
                "name": "Полное описание",
                "description": long_description,
                "summary": long_summary,
            }
        ],
    )

    assert long_description in user_p
    assert long_summary in user_p
    assert "…" not in user_p


def test_build_transfer_participants_includes_owner_only_when_needed() -> None:
    clients = [
        {
            "id": "C1",
            "full_name": "Иванов Иван Иванович",
            "traits": "резидент",
            "description": "основной клиент",
        }
    ]
    accounts = [{"id": "A1", "client": "C1", "currency": "USD", "description": "счёт"}]
    cards = [{"id": "K1", "account": "A1", "description": "карта"}]

    sys_no, user_no = PromptBuilder().build_transfer_participants(
        request="перевод", clients=clients, accounts=accounts, cards=cards,
        need_account_owner=False,
    )
    assert "accountOwner" not in sys_no
    # All three catalogs are present with short ids.
    assert "C1 | Иванов Иван Иванович | резидент | основной клиент" in user_no
    assert "A1 | C1 | USD | счёт" in user_no
    assert "K1 | A1 | карта" in user_no
    assert "фамилия или ФИО" in user_no

    sys_yes, _ = PromptBuilder().build_transfer_participants(
        request="перевод", clients=clients, accounts=accounts, cards=cards,
        need_account_owner=True,
    )
    assert "accountOwner" in sys_yes


# --- DB-backed overrides ---


def test_override_replaces_system_prompt() -> None:
    sys_p, _, _ = PromptBuilder(
        {"field_mapping": "МОЯ ИНСТРУКЦИЯ"}
    ).build_template_field_mapping(
        leaves=[{"location": "/x", "value": "1"}], catalog=[]
    )
    assert sys_p == "МОЯ ИНСТРУКЦИЯ"


def test_blank_or_missing_override_falls_back_to_default() -> None:
    default = PROMPT_DEFS["template_meta"].default
    # Missing key -> default.
    sys_missing, _ = PromptBuilder().build_template_meta(content="<a/>", fmt="xml")
    assert sys_missing == default
    # Blank / whitespace override -> default (treated as "no override").
    sys_blank, _ = PromptBuilder({"template_meta": "   "}).build_template_meta(
        content="<a/>", fmt="xml"
    )
    assert sys_blank == default


def test_participants_override_substitutes_placeholders_without_breaking_json() -> None:
    override = (
        "Роли: $roles.\n$owner_rule"
        '{"sender":{"client":"C1"}$owner_answer}\n'
    )
    builder = PromptBuilder({"transfer_participants": override})
    common: dict[str, Any] = dict(request="перевод", clients=[], accounts=[], cards=[])

    sys_no, _ = builder.build_transfer_participants(**common, need_account_owner=False)
    # $roles substituted, owner placeholders collapse to empty, JSON braces intact.
    assert "sender (отправитель/плательщик), receiver (получатель)" in sys_no
    assert "accountOwner" not in sys_no
    assert '{"sender":{"client":"C1"}}' in sys_no
    assert "$" not in sys_no  # all known placeholders consumed

    sys_yes, _ = builder.build_transfer_participants(**common, need_account_owner=True)
    assert "accountOwner" in sys_yes
    assert '"accountOwner":{"client":"C3"' in sys_yes


async def test_load_prompt_overrides_returns_only_nonblank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = {
        "llm_prompt:field_mapping": "МОЙ ПРОМПТ",
        "llm_prompt:template_meta": "   ",  # blank → skipped
        # transfer_pick / transfer_participants absent → skipped
    }

    class _FakeRepo:
        def __init__(self, session: Any) -> None:
            self._store = store

        async def get(self, key: str, default: Any = None) -> Any:
            return self._store.get(key, default)

    monkeypatch.setattr(settings_repo_mod, "SettingsRepository", _FakeRepo)

    overrides = await load_prompt_overrides(session=cast(AsyncSession, object()))
    assert overrides == {"field_mapping": "МОЙ ПРОМПТ"}


# --- Prompt save-time validation ---


def test_validate_prompt_text_accepts_known_placeholders() -> None:
    # Should not raise.
    validate_prompt_text(
        "transfer_participants", "Роли: $roles.\n$owner_rule X$owner_answer"
    )


def test_validate_prompt_text_rejects_unknown_placeholder() -> None:
    with pytest.raises(ValidationFailed):
        validate_prompt_text("transfer_participants", "Роль: $foo")


def test_validate_prompt_text_rejects_stray_dollar() -> None:
    with pytest.raises(ValidationFailed):
        validate_prompt_text("transfer_participants", "сумма $100")


def test_validate_prompt_text_ignores_non_participants_prompts() -> None:
    # The other three prompts are not templated — a bare $ is fine there.
    validate_prompt_text("template_meta", "сумма $100 и $foo")
    validate_prompt_text("field_mapping", "$bar")


def test_validate_prompt_text_allows_blank() -> None:
    validate_prompt_text("transfer_participants", "   ")


def test_default_participants_prompt_passes_validation() -> None:
    # Re-saving the coded default must be accepted.
    validate_prompt_text(
        "transfer_participants", PROMPT_DEFS["transfer_participants"].default
    )
