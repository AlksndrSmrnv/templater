from __future__ import annotations

import json

from app.llm.prompts import PromptBuilder


def test_build_template_field_mapping_includes_payload_keys() -> None:
    sys_p, user_p = PromptBuilder.build_template_field_mapping(
        content='{"fullName":"X"}',
        fmt="json",
        leaves=["/fullName"],
        catalog=[{"path": "sender.fullName", "label": "S", "data_type": "string"}],
    )
    payload = json.loads(user_p)

    assert "JSON" in sys_p
    assert "placeholders" in sys_p
    assert "accountOwner.*" in sys_p
    assert "ownerName" in sys_p
    assert "JSON Pointer" in sys_p
    assert '"location": "/RqHdr/RqUID"' in sys_p
    assert "побайтово равной" in sys_p
    assert payload["leaves"] == ["/fullName"]
    assert '"fullName": "X"' in user_p or 'fullName' in user_p
    assert '"sender.fullName"' in user_p


def test_build_template_field_mapping_preserves_xml_leaf_path_syntax() -> None:
    sys_p, user_p = PromptBuilder.build_template_field_mapping(
        content="<Root><Item id=\"1\"><Name>X</Name></Item></Root>",
        fmt="xml",
        leaves=["/Root/Item[0]/@id", "/Root/Item[0]/Name[0]/#text"],
        catalog=[{"path": "sender.fullName", "label": "S", "data_type": "string"}],
    )
    payload = json.loads(user_p)

    assert payload["leaves"] == ["/Root/Item[0]/@id", "/Root/Item[0]/Name[0]/#text"]
    assert "[idx]" in sys_p
    assert "#text" in sys_p
    assert "@attr" in sys_p
    assert "не нормализуй" in sys_p


def test_build_template_field_mapping_defines_roles_and_account_owner_rule() -> None:
    sys_p, _ = PromptBuilder.build_template_field_mapping(
        content='{"ownerName":"X"}',
        fmt="json",
        leaves=["/ownerName"],
        catalog=[{"path": "accountOwner.ownerName", "label": "Owner", "data_type": "string"}],
    )

    assert "В системе три РОЛИ участников" in sys_p
    assert "sender.* — отправитель" in sys_p
    assert "receiver.* — получатель" in sys_p
    assert "accountOwner.* — ВЛАДЕЛЕЦ СЧЁТА" in sys_p
    assert "Не сворачивай такого участника в sender или receiver" in sys_p


def test_build_template_field_mapping_skips_service_identifiers() -> None:
    sys_p, _ = PromptBuilder.build_template_field_mapping(
        content='{"operuid":"1","rquid":"2"}',
        fmt="json",
        leaves=[
            "/operuid",
            "/rquid",
        ],
        catalog=[{"path": "sender.ucp_id", "label": "Sender UCP ID", "data_type": "string"}],
    )

    assert "rqUID" in sys_p
    assert "operUID" in sys_p
    assert "rqTm" in sys_p
    assert "channelDateTime" in sys_p
    assert "{{rqUID}}" in sys_p
    assert "динамические" in sys_p


def test_build_template_field_mapping_requests_precise_transfer_summary() -> None:
    sys_p, _ = PromptBuilder.build_template_field_mapping(
        content='{"productId":"another_int"}',
        fmt="json",
        leaves=["/productId"],
        catalog=[],
    )

    assert "2–4 предложения" in sys_p
    assert "productId" in sys_p
    assert "another_int" in sys_p
    assert "another_ext" in sys_p
    assert "Подразделение-источник" in sys_p
    assert "Комиссию" in sys_p


def test_build_template_field_mapping_compacts_json_and_removes_null_object_keys() -> None:
    _, user_p = PromptBuilder.build_template_field_mapping(
        content=(
            '{\r\n'
            '\t"fullName": "X",\r\n'
            '\t"empty": null,\r\n'
            '\t"nested": {"dropMe": null, "keepMe": "Y"},\r\n'
            '\t"items": [null, {"dropMe": null, "keepMe": "Z"}]\r\n'
            '}'
        ),
        fmt="json",
        leaves=["/fullName", "/nested/keepMe", "/items/0", "/items/1/keepMe"],
        catalog=[],
    )
    payload = json.loads(user_p)

    assert payload["template"] == (
        '{"fullName":"X","nested":{"keepMe":"Y"},"items":[null,{"keepMe":"Z"}]}'
    )
    assert "\t" not in payload["template"]
    assert "\r\n" not in payload["template"]


def test_build_template_field_mapping_keeps_invalid_json_as_is() -> None:
    content = '{"fullName": "X",'

    _, user_p = PromptBuilder.build_template_field_mapping(
        content=content,
        fmt="json",
        leaves=["/fullName"],
        catalog=[],
    )
    payload = json.loads(user_p)

    assert payload["template"] == content


def test_build_template_field_mapping_slims_catalog_for_prompt() -> None:
    _, user_p = PromptBuilder.build_template_field_mapping(
        content='{"fullName":"X"}',
        fmt="json",
        leaves=["/fullName"],
        catalog=[
            {"path": "sender.fullName", "label": "Sender — ФИО", "data_type": "string"},
        ],
    )
    payload = json.loads(user_p)

    assert payload["catalog"] == [{"path": "sender.fullName", "label": "Sender — ФИО"}]
    assert "data_type" not in user_p


def test_build_template_field_mapping_requires_only_successful_suggestions() -> None:
    sys_p, _ = PromptBuilder.build_template_field_mapping(
        content='{"fullName":"X"}',
        fmt="json",
        leaves=["/fullName"],
        catalog=[],
    )

    assert "включай ТОЛЬКО те `location`" in sys_p
    assert "НЕ возвращай эту запись вовсе" in sys_p
    assert "null" not in sys_p
    assert '"suggestion": str' in sys_p


def test_build_template_meta_returns_strings() -> None:
    sys_p, user_p = PromptBuilder.build_template_meta(content="<a/>", fmt="xml")
    assert isinstance(sys_p, str) and sys_p
    assert "productId" in sys_p
    assert "another_ext" in sys_p
    assert "xml" in user_p
