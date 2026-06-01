from __future__ import annotations

from app.llm.prompts import PromptBuilder


def _mapping(
    leaves: list[dict[str, str]], catalog: list[dict[str, str]]
) -> tuple[str, str, dict[str, str]]:
    return PromptBuilder.build_template_field_mapping(leaves=leaves, catalog=catalog)


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


def test_build_template_field_mapping_has_few_shot_example() -> None:
    sys_p, _, _ = _mapping(
        leaves=[{"location": "/Payer/INN", "value": "7701234567"}],
        catalog=[{"path": "sender.inn", "label": "Sender — ИНН", "data_type": "string"}],
    )

    assert "Пример." in sys_p
    assert '{"placeholders":[{"leaf":"L1","field":"sender.inn"}]}' in sys_p
    # Demonstrates skipping a leaf that has no matching field.
    assert "пропущен" in sys_p


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
    sys_p, user_p = PromptBuilder.build_template_meta(content="<a/>", fmt="xml")

    assert isinstance(sys_p, str) and sys_p
    assert "productId" in sys_p
    assert "another_ext" in sys_p
    assert user_p.startswith("Формат: xml")
    assert "<a/>" in user_p
    # Plain text, not a json.dumps payload wrapper.
    assert '"template"' not in user_p and '"format"' not in user_p


def test_build_template_meta_compacts_json_and_removes_null_object_keys() -> None:
    _, user_p = PromptBuilder.build_template_meta(
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
