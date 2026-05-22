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


def test_build_template_meta_returns_strings() -> None:
    sys_p, user_p = PromptBuilder.build_template_meta(content="<a/>", fmt="xml")
    assert isinstance(sys_p, str) and sys_p
    assert "xml" in user_p
