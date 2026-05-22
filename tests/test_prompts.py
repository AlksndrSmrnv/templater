from __future__ import annotations

from app.llm.prompts import PromptBuilder


def test_build_template_field_mapping_includes_payload_keys() -> None:
    sys_p, user_p = PromptBuilder.build_template_field_mapping(
        content='{"fullName":"X"}',
        fmt="json",
        leaves=[{"location": "/fullName", "value": "X"}],
        catalog=[{"path": "sender.fullName", "label": "S", "data_type": "string"}],
    )
    assert "JSON" in sys_p
    assert "placeholders" in sys_p
    assert "accountOwner.*" in sys_p
    assert "ownerName" in sys_p
    assert '"fullName": "X"' in user_p or 'fullName' in user_p
    assert '"sender.fullName"' in user_p


def test_build_template_field_mapping_defines_roles_and_account_owner_rule() -> None:
    sys_p, _ = PromptBuilder.build_template_field_mapping(
        content='{"ownerName":"X"}',
        fmt="json",
        leaves=[{"location": "/ownerName", "value": "X"}],
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
            {"location": "/operuid", "value": "1"},
            {"location": "/rquid", "value": "2"},
        ],
        catalog=[{"path": "sender.ucp_id", "label": "Sender UCP ID", "data_type": "string"}],
    )

    assert "operuid" in sys_p
    assert "rquid" in sys_p
    assert "служебные идентификаторы" in sys_p


def test_build_template_meta_returns_strings() -> None:
    sys_p, user_p = PromptBuilder.build_template_meta(content="<a/>", fmt="xml")
    assert isinstance(sys_p, str) and sys_p
    assert "xml" in user_p
