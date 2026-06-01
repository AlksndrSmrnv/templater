from __future__ import annotations

import json
import logging
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.db.models import MessageTemplate
from app.services.templates import (
    TemplateService,
    normalize_placeholders,
    placeholders_have_account_owner,
)
from app.utils import walker
from app.utils.errors import ValidationFailed


def test_normalize_placeholders_accepts_valid_entries() -> None:
    raw = [
        {"location": "/a", "mode": "mapped", "value": "{{sender.x}}", "original": "X"},
        {"location": "/b", "mode": "literal", "value": "lit"},  # original defaults to ""
        {"location": "/c", "mode": "dynamic", "value": "{{rqUID}}", "original": "old"},
    ]
    out = normalize_placeholders(raw)
    assert out[0]["location"] == "/a"
    assert out[1]["original"] == ""
    assert out[1]["mode"] == "literal"
    assert out[2]["mode"] == "dynamic"


def test_normalize_placeholders_rejects_bad_mode() -> None:
    with pytest.raises(ValidationFailed):
        normalize_placeholders([{"location": "/a", "mode": "weird", "value": "v"}])


def test_normalize_placeholders_rejects_missing_location() -> None:
    with pytest.raises(ValidationFailed):
        normalize_placeholders([{"mode": "literal", "value": "v"}])


def test_normalize_placeholders_rejects_non_list_and_non_dict() -> None:
    with pytest.raises(ValidationFailed):
        normalize_placeholders({"location": "/a"})
    with pytest.raises(ValidationFailed):
        normalize_placeholders(["not-a-dict"])


def test_normalize_placeholders_none_is_empty() -> None:
    assert normalize_placeholders(None) == []


def test_placeholders_have_account_owner_detects_suggestions_and_values() -> None:
    assert placeholders_have_account_owner(
        [{"location": "/ownerName", "suggestion": "accountOwner.ownerName", "value": ""}]
    )
    assert placeholders_have_account_owner(
        [{"location": "/account", "value": "{{ accountOwner.account.number }}"}]
    )
    assert placeholders_have_account_owner(
        [{"location": "/card", "value": "accountOwner.card.number"}]
    )
    assert not placeholders_have_account_owner(
        [{"location": "/fullName", "suggestion": "sender.fullName", "value": "{{sender.fullName}}"}]
    )


def test_regenerate_content_uses_original_content_as_source() -> None:
    """If original_content drifts out of sync with content, regenerate uses
    the (now stale) original. This documents why update() must keep
    original_content in lockstep with content on PUT."""

    template = SimpleNamespace(
        format="json",
        content='{"a": "{{sender.name}}"}',
        original_content='{"a": "Старое"}',
        placeholders=[
            {
                "location": "/a",
                "mode": "mapped",
                "value": "{{sender.name}}",
                "original": "Старое",
            }
        ],
    )
    result = TemplateService.regenerate_content(cast(MessageTemplate, template))
    parsed = json.loads(result)
    assert parsed["a"] == "{{sender.name}}"


def test_regenerate_content_returns_original_when_no_placeholders() -> None:
    template = SimpleNamespace(
        format="json",
        content="will-be-ignored",
        original_content='{"a": 1}',
        placeholders=[],
    )
    assert TemplateService.regenerate_content(cast(MessageTemplate, template)) == '{"a": 1}'


def test_regenerate_content_preserves_dynamic_placeholders() -> None:
    template = SimpleNamespace(
        format="json",
        content='{"rqUID": "old"}',
        original_content='{"rqUID": "old"}',
        placeholders=[
            {
                "location": "/rqUID",
                "mode": "dynamic",
                "value": "{{rqUID}}",
                "original": "old",
                "suggestion": "rqUID",
            }
        ],
    )

    result = TemplateService.regenerate_content(cast(MessageTemplate, template))

    assert json.loads(result)["rqUID"] == "{{rqUID}}"


def test_heuristic_mappings_matches_by_tail() -> None:
    leaves = [
        type("L", (), {"location": "/fullName", "value": "Иванов"})(),
        type("L", (), {"location": "/passport/series", "value": "4510"})(),
        type("L", (), {"location": "/notes", "value": "Что-то"})(),
    ]
    catalog = [
        {"path": "sender.fullName", "label": "Sender — ФИО", "data_type": "string"},
        {"path": "sender.account.series", "label": "Sender — Серия", "data_type": "string"},
    ]
    out = TemplateService._heuristic_mappings(leaves, catalog)
    assert out["/fullName"]["suggestion"] == "sender.fullName"
    assert out["/passport/series"]["suggestion"] == "sender.account.series"
    assert "/notes" not in out


@pytest.mark.asyncio
async def test_analyze_content_returns_preview_without_mutating_model() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [{"path": "sender.fullName", "label": "Sender — ФИО", "data_type": "string"}]

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content='{"fullName": "Иванов", "note": "x"}',
        llm_service=None,
    )

    assert json.loads(result["content"])["fullName"] == "{{sender.fullName}}"
    assert result["placeholders"][0]["location"] == "/fullName"
    assert result["placeholders"][0]["mode"] == "mapped"
    assert result["llm_meta"]["summary"].startswith("Анализ выполнен без LLM")
    assert result["llm_debug"] is None


@pytest.mark.asyncio
async def test_analyze_persists_llm_debug_on_template() -> None:
    # The debug must be stored on the template so it stays viewable after the
    # fact — e.g. once a template was processed in bulk from the collections menu.
    class FakeSession:
        async def flush(self) -> None:
            return None

    svc = TemplateService(cast(Any, FakeSession()))
    debug = {"system_prompt": "sys", "user_prompt": "usr", "response_text": "resp"}

    async def fake_analyze_content(
        *, fmt: str, original_content: str, llm_service: Any | None = None
    ) -> dict[str, Any]:
        return {
            "content": original_content,
            "placeholders": [],
            "llm_meta": {"summary": "ok"},
            "llm_debug": debug,
        }

    svc.analyze_content = fake_analyze_content  # type: ignore[method-assign]

    template = SimpleNamespace(
        format="json",
        content='{"a":"x"}',
        original_content='{"a":"x"}',
        placeholders=[],
        llm_meta={},
        llm_debug=None,
    )

    await svc.analyze(cast(Any, template))

    assert template.llm_debug == debug


@pytest.mark.asyncio
async def test_analyze_content_propagates_llm_debug() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))
    debug = {
        "system_prompt": "system",
        "user_prompt": "user",
        "response_text": '{"meta": {"summary": "llm"}, "placeholders": []}',
    }

    async def fake_catalog() -> list[dict[str, str]]:
        return []

    class FakeLlm:
        async def analyze_template(
            self,
            *,
            content: str,
            fmt: str,
            leaves: list[dict[str, str]],
            catalog: list[dict[str, str]],
        ) -> dict[str, Any]:
            assert leaves == [{"location": "/note", "value": "x"}]
            return {"meta": {"summary": "llm"}, "placeholders": [], "debug": debug}

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content='{"note": "x"}',
        llm_service=FakeLlm(),
    )

    assert result["llm_meta"]["summary"] == "llm"
    assert result["llm_debug"] == debug


def test_llm_mappings_by_leaf_suffix_matches_unique_same_role_dotted_location() -> None:
    leaves = [
        walker.Leaf(
            location="/root/accountOwner/client/clientInfo/personName/lastName",
            value="Иванов",
        )
    ]
    raw_placeholders = [
        {
            "location": "template.root.accountOwner.client.clientInfo.personName.lastName",
            "suggestion": "accountOwner.surname",
        }
    ]

    result = TemplateService._llm_mappings_by_leaf(leaves, raw_placeholders)

    assert result[leaves[0].location]["suggestion"] == "accountOwner.surname"


def test_llm_mappings_by_leaf_suffix_does_not_match_duplicate_leaf_tail_role() -> None:
    leaves = [
        walker.Leaf(location="/root/accountOwner/client/primary/lastName", value="Иванов"),
        walker.Leaf(location="/root/accountOwner/client/backup/lastName", value="Петров"),
    ]
    raw_placeholders = [
        {
            "location": "template.root.accountOwner.client.primary.lastName",
            "suggestion": "accountOwner.surname",
        }
    ]

    result = TemplateService._llm_mappings_by_leaf(leaves, raw_placeholders)

    assert result == {}


def test_llm_mappings_by_leaf_suffix_does_not_match_duplicate_llm_tail_role() -> None:
    leaves = [
        walker.Leaf(
            location="/root/accountOwner/client/clientInfo/personName/lastName",
            value="Иванов",
        )
    ]
    raw_placeholders = [
        {
            "location": "template.root.accountOwner.client.clientInfo.personName.lastName",
            "suggestion": "accountOwner.surname",
        },
        {
            "location": "payload.accountOwner.personName.lastName",
            "suggestion": "accountOwner.surname",
        },
    ]

    result = TemplateService._llm_mappings_by_leaf(leaves, raw_placeholders)

    assert result == {}


def test_llm_mappings_by_leaf_suffix_ignores_paths_without_role() -> None:
    leaves = [walker.Leaf(location="/root/personName/lastName", value="Иванов")]
    raw_placeholders = [
        {"location": "template.root.personName.lastName", "suggestion": "accountOwner.surname"}
    ]

    result = TemplateService._llm_mappings_by_leaf(leaves, raw_placeholders)

    assert result == {}


def test_llm_mappings_by_leaf_warns_once_about_unmatched_placeholders(
    caplog: pytest.LogCaptureFixture,
) -> None:
    leaves = [walker.Leaf(location="/sender/firstName", value="Иван")]
    raw_placeholders = [
        {"location": "/receiver/firstName", "suggestion": "receiver.firstName"},
        {"location": "/accountOwner/lastName", "suggestion": "accountOwner.surname"},
    ]

    with caplog.at_level(logging.WARNING, logger="app.services.templates"):
        result = TemplateService._llm_mappings_by_leaf(leaves, raw_placeholders)

    assert result == {}
    assert len(caplog.records) == 1
    assert "LLM returned 2 unmatched placeholders" in caplog.text
    assert "/receiver/firstName" in caplog.text
    assert "receiver.firstName" in caplog.text
    assert "/accountOwner/lastName" in caplog.text
    assert "accountOwner.surname" in caplog.text


def test_llm_mappings_by_leaf_logs_malformed_placeholder_entries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    leaves = [walker.Leaf(location="/sender/firstName", value="Иван")]
    raw_placeholders = [
        "not-a-dict",
        {},
        {"location": ""},
        {"location": None},
        {"location": 123},
    ]

    with caplog.at_level(logging.INFO, logger="app.services.templates"):
        result = TemplateService._llm_mappings_by_leaf(leaves, raw_placeholders)

    assert result == {}
    assert len(caplog.records) == 1
    assert "LLM returned 5 malformed placeholder entries (dropped)" in caplog.text


@pytest.mark.asyncio
async def test_analyze_content_sets_account_owner_meta_when_placeholder_detected() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [
            {"path": "sender.ownerName", "label": "Sender — Владелец", "data_type": "string"},
            {
                "path": "accountOwner.ownerName",
                "label": "Owner — Владелец",
                "data_type": "string",
            },
        ]

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content='{"ownerName": "Иванов"}',
        llm_service=None,
    )

    assert json.loads(result["content"])["ownerName"] == "{{accountOwner.ownerName}}"
    assert result["placeholders"][0]["suggestion"] == "accountOwner.ownerName"
    assert result["llm_meta"]["has_account_owner"] is True


@pytest.mark.asyncio
async def test_analyze_content_sets_account_owner_meta_false_without_placeholder() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [{"path": "sender.fullName", "label": "Sender — ФИО", "data_type": "string"}]

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content='{"fullName": "Иванов"}',
        llm_service=None,
    )

    assert result["placeholders"][0]["suggestion"] == "sender.fullName"
    assert result["llm_meta"]["has_account_owner"] is False


@pytest.mark.asyncio
async def test_analyze_content_uses_role_from_path_to_override_heuristic_role() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [
            {"path": "sender.firstName", "label": "Sender — Имя", "data_type": "string"},
            {"path": "receiver.firstName", "label": "Receiver — Имя", "data_type": "string"},
        ]

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content='{"recipient": {"firstName": "Иван"}}',
        llm_service=None,
    )

    assert json.loads(result["content"])["recipient"]["firstName"] == "{{receiver.firstName}}"
    assert result["placeholders"][0]["suggestion"] == "receiver.firstName"


@pytest.mark.asyncio
async def test_analyze_content_leaves_unmatched_field_without_path_role_literal() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [{"path": "sender.ucp_id", "label": "Sender — UCP ID", "data_type": "string"}]

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content='{"messageId": "service-id"}',
        llm_service=None,
    )

    assert json.loads(result["content"])["messageId"] == "service-id"
    assert result["placeholders"][0]["mode"] == "literal"
    assert result["placeholders"][0]["suggestion"] is None


@pytest.mark.asyncio
async def test_analyze_content_marks_dynamic_system_fields_before_role_mapping() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [{"path": "sender.rqUID", "label": "Sender — rqUID", "data_type": "string"}]

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content=(
            '{"sender": {"RqUID": "old-rq"}, "oper_uid": "old-oper",'
            ' "rqTm": "2025-01-01T00:00:00Z", "channel_date_time": "2025-01-01T00:00:01Z"}'
        ),
        llm_service=None,
    )

    parsed = json.loads(result["content"])
    by_location = {item["location"]: item for item in result["placeholders"]}

    assert parsed["sender"]["RqUID"] == "{{rqUID}}"
    assert parsed["oper_uid"] == "{{operUID}}"
    assert parsed["rqTm"] == "{{rqTm}}"
    assert parsed["channel_date_time"] == "{{channelDateTime}}"
    assert by_location["/sender/RqUID"] == {
        "location": "/sender/RqUID",
        "original": "old-rq",
        "mode": "dynamic",
        "value": "{{rqUID}}",
        "suggestion": "rqUID",
    }
    assert by_location["/oper_uid"]["mode"] == "dynamic"
    assert by_location["/oper_uid"]["suggestion"] == "operUID"


@pytest.mark.asyncio
async def test_analyze_content_uses_path_role_with_llm_attribute() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [
            {"path": "sender.firstName", "label": "Sender — Имя", "data_type": "string"},
            {"path": "receiver.firstName", "label": "Receiver — Имя", "data_type": "string"},
        ]

    class FakeLlm:
        async def analyze_template(
            self,
            *,
            content: str,
            fmt: str,
            leaves: list[dict[str, str]],
            catalog: list[dict[str, str]],
        ) -> dict[str, Any]:
            return {
                "meta": {"summary": "llm"},
                "placeholders": [{"location": "/recipient/firstName", "suggestion": "sender.firstName"}],
            }

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content='{"recipient": {"firstName": "Иван"}}',
        llm_service=FakeLlm(),
    )

    assert result["placeholders"][0]["suggestion"] == "receiver.firstName"


@pytest.mark.asyncio
async def test_analyze_content_resolves_llm_suggestion_case_insensitively() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [{"path": "receiver.firstName", "label": "Receiver — Имя", "data_type": "string"}]

    class FakeLlm:
        async def analyze_template(
            self,
            *,
            content: str,
            fmt: str,
            leaves: list[dict[str, str]],
            catalog: list[dict[str, str]],
        ) -> dict[str, Any]:
            return {
                "meta": {"summary": "llm"},
                "placeholders": [{"location": "/recipient/firstName", "suggestion": "RECEIVER.FirstName"}],
            }

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content='{"recipient": {"firstName": "Иван"}}',
        llm_service=FakeLlm(),
    )

    assert result["placeholders"][0]["suggestion"] == "receiver.firstName"
    assert json.loads(result["content"])["recipient"]["firstName"] == "{{receiver.firstName}}"


@pytest.mark.asyncio
async def test_analyze_content_keeps_account_owner_role_for_llm_ucp_id_attribute() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [
            {"path": "sender.ucp_id", "label": "Sender — UCP ID", "data_type": "string"},
            {"path": "accountOwner.ucp_id", "label": "Owner — UCP ID", "data_type": "string"},
        ]

    class FakeLlm:
        async def analyze_template(
            self,
            *,
            content: str,
            fmt: str,
            leaves: list[dict[str, str]],
            catalog: list[dict[str, str]],
        ) -> dict[str, Any]:
            return {
                "meta": {"summary": "llm"},
                "placeholders": [
                    {
                        "location": "/accountOwner/client/clientId",
                        "suggestion": "sender.ucp_id",
                    }
                ],
            }

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content='{"accountOwner": {"client": {"clientId": "123"}}}',
        llm_service=FakeLlm(),
    )

    assert result["placeholders"][0]["suggestion"] == "accountOwner.ucp_id"
    assert json.loads(result["content"])["accountOwner"]["client"]["clientId"] == "{{accountOwner.ucp_id}}"


@pytest.mark.asyncio
async def test_analyze_content_falls_back_for_account_owner_llm_suggestion_without_attribute() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [
            {"path": "sender.fullName", "label": "Sender — ФИО", "data_type": "string"},
            {"path": "receiver.fullName", "label": "Receiver — ФИО", "data_type": "string"},
            {"path": "accountOwner.fullName", "label": "Owner — ФИО", "data_type": "string"},
        ]

    class FakeLlm:
        async def analyze_template(
            self,
            *,
            content: str,
            fmt: str,
            leaves: list[dict[str, str]],
            catalog: list[dict[str, str]],
        ) -> dict[str, Any]:
            return {
                "meta": {"summary": "llm"},
                "placeholders": [
                    {
                        "location": "/accountOwner/client/fullName",
                        "suggestion": "accountOwner",
                    }
                ],
            }

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content='{"accountOwner": {"client": {"fullName": "Смирнов"}}}',
        llm_service=FakeLlm(),
    )

    assert result["placeholders"][0]["suggestion"] == "accountOwner.fullName"
    assert (
        json.loads(result["content"])["accountOwner"]["client"]["fullName"]
        == "{{accountOwner.fullName}}"
    )


@pytest.mark.asyncio
async def test_analyze_content_falls_back_for_account_owner_nested_llm_suggestion() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [
            {"path": "sender.fullName", "label": "Sender — ФИО", "data_type": "string"},
            {"path": "receiver.fullName", "label": "Receiver — ФИО", "data_type": "string"},
            {"path": "accountOwner.fullName", "label": "Owner — ФИО", "data_type": "string"},
        ]

    class FakeLlm:
        async def analyze_template(
            self,
            *,
            content: str,
            fmt: str,
            leaves: list[dict[str, str]],
            catalog: list[dict[str, str]],
        ) -> dict[str, Any]:
            return {
                "meta": {"summary": "llm"},
                "placeholders": [
                    {
                        "location": "/accountOwner/client/fullName",
                        "suggestion": "accountOwner.client.fullName",
                    }
                ],
            }

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content='{"accountOwner": {"client": {"fullName": "Смирнов"}}}',
        llm_service=FakeLlm(),
    )

    assert result["placeholders"][0]["suggestion"] == "accountOwner.fullName"
    assert (
        json.loads(result["content"])["accountOwner"]["client"]["fullName"]
        == "{{accountOwner.fullName}}"
    )


@pytest.mark.asyncio
async def test_analyze_content_falls_back_for_account_owner_synonym_llm_suggestion() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [
            {"path": "sender.fullName", "label": "Sender — ФИО", "data_type": "string"},
            {"path": "receiver.fullName", "label": "Receiver — ФИО", "data_type": "string"},
            {"path": "accountOwner.fullName", "label": "Owner — ФИО", "data_type": "string"},
        ]

    class FakeLlm:
        async def analyze_template(
            self,
            *,
            content: str,
            fmt: str,
            leaves: list[dict[str, str]],
            catalog: list[dict[str, str]],
        ) -> dict[str, Any]:
            return {
                "meta": {"summary": "llm"},
                "placeholders": [
                    {
                        "location": "/accountOwner/client/fullName",
                        "suggestion": "owner",
                    }
                ],
            }

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content='{"accountOwner": {"client": {"fullName": "Смирнов"}}}',
        llm_service=FakeLlm(),
    )

    assert result["placeholders"][0]["suggestion"] == "accountOwner.fullName"
    assert (
        json.loads(result["content"])["accountOwner"]["client"]["fullName"]
        == "{{accountOwner.fullName}}"
    )


@pytest.mark.asyncio
async def test_analyze_content_maps_deep_nested_account_owner_field_by_role_not_exact_path() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [
            {"path": "sender.firstName", "label": "Sender — Имя", "data_type": "string"},
            {"path": "receiver.firstName", "label": "Receiver — Имя", "data_type": "string"},
            {"path": "accountOwner.firstName", "label": "Owner — Имя", "data_type": "string"},
        ]

    class FakeLlm:
        async def analyze_template(
            self,
            *,
            content: str,
            fmt: str,
            leaves: list[dict[str, str]],
            catalog: list[dict[str, str]],
        ) -> dict[str, Any]:
            return {
                "meta": {"summary": "llm"},
                "placeholders": [
                    {
                        "location": (
                            "/тут_путь_до_accountOwner/accountOwner/client/clientInfo/personInfo/"
                            "personName/firstName"
                        ),
                        "suggestion": (
                            "accountOwner.accountOwner.client.clientInfo.personInfo.personName.firstName"
                        ),
                    }
                ],
            }

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content=(
            '{"тут_путь_до_accountOwner": {"accountOwner": {"client": {"clientInfo": '
            '{"personInfo": {"personName": {"firstName": "Иван"}}}}}}}'
        ),
        llm_service=FakeLlm(),
    )

    parsed = json.loads(result["content"])

    assert result["placeholders"][0]["suggestion"] == "accountOwner.firstName"
    assert (
        parsed["тут_путь_до_accountOwner"]["accountOwner"]["client"]["clientInfo"]["personInfo"][
            "personName"
        ]["firstName"]
        == "{{accountOwner.firstName}}"
    )


@pytest.mark.asyncio
async def test_analyze_content_maps_account_owner_surname_from_exact_llm_location() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [
            {"path": "sender.surname", "label": "Sender — Фамилия", "data_type": "string"},
            {"path": "receiver.surname", "label": "Receiver — Фамилия", "data_type": "string"},
            {"path": "accountOwner.surname", "label": "Owner — Фамилия", "data_type": "string"},
        ]

    class FakeLlm:
        async def analyze_template(
            self,
            *,
            content: str,
            fmt: str,
            leaves: list[dict[str, str]],
            catalog: list[dict[str, str]],
        ) -> dict[str, Any]:
            return {
                "meta": {"summary": "llm"},
                "placeholders": [
                    {
                        "location": (
                            "/тут_путь_до_accountOwner/accountOwner/client/clientInfo/"
                            "personName/lastName"
                        ),
                        "suggestion": "accountOwner.surname",
                    }
                ],
            }

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content=(
            '{"тут_путь_до_accountOwner": {"accountOwner": {"client": {"clientInfo": '
            '{"personName": {"lastName": "Иванов"}}}}}}'
        ),
        llm_service=FakeLlm(),
    )

    by_location = {item["location"]: item for item in result["placeholders"]}
    target = by_location[
        "/тут_путь_до_accountOwner/accountOwner/client/clientInfo/personName/lastName"
    ]
    parsed = json.loads(result["content"])

    assert target["mode"] == "mapped"
    assert target["suggestion"] == "accountOwner.surname"
    assert (
        parsed["тут_путь_до_accountOwner"]["accountOwner"]["client"]["clientInfo"]["personName"][
            "lastName"
        ]
        == "{{accountOwner.surname}}"
    )


@pytest.mark.asyncio
async def test_analyze_content_uses_nested_account_owner_llm_leaf_attribute() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [
            {"path": "sender.firstName", "label": "Sender — Имя", "data_type": "string"},
            {"path": "accountOwner.firstName", "label": "Owner — Имя", "data_type": "string"},
        ]

    class FakeLlm:
        async def analyze_template(
            self,
            *,
            content: str,
            fmt: str,
            leaves: list[dict[str, str]],
            catalog: list[dict[str, str]],
        ) -> dict[str, Any]:
            return {
                "meta": {"summary": "llm"},
                "placeholders": [
                    {
                        "location": "/root/accountOwner/client/arbitraryWrapper/given",
                        "suggestion": (
                            "accountOwner.client.anyIntermediatePath.personName.firstName"
                        ),
                    }
                ],
            }

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content=(
            '{"root": {"accountOwner": {"client": {"arbitraryWrapper": {"given": "Иван"}}}}}'
        ),
        llm_service=FakeLlm(),
    )

    parsed = json.loads(result["content"])

    assert result["placeholders"][0]["suggestion"] == "accountOwner.firstName"
    assert (
        parsed["root"]["accountOwner"]["client"]["arbitraryWrapper"]["given"]
        == "{{accountOwner.firstName}}"
    )


@pytest.mark.asyncio
async def test_analyze_content_maps_account_owner_field_without_llm_by_path_role() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [
            {"path": "sender.firstName", "label": "Sender — Имя", "data_type": "string"},
            {"path": "accountOwner.firstName", "label": "Owner — Имя", "data_type": "string"},
        ]

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content=(
            '{"outer": {"accountOwner": {"client": {"unknownEnvelope": {"firstName": "Иван"}}}}}'
        ),
        llm_service=None,
    )

    parsed = json.loads(result["content"])

    assert result["placeholders"][0]["suggestion"] == "accountOwner.firstName"
    assert (
        parsed["outer"]["accountOwner"]["client"]["unknownEnvelope"]["firstName"]
        == "{{accountOwner.firstName}}"
    )


@pytest.mark.asyncio
async def test_analyze_content_preserves_account_owner_account_scope_from_path() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [
            {"path": "accountOwner.number", "label": "Owner — Номер клиента", "data_type": "string"},
            {
                "path": "accountOwner.account.number",
                "label": "Owner account — Номер счёта",
                "data_type": "string",
            },
        ]

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content=(
            '{"root": {"accountOwner": {"wrapper": {"account": {"details": {"number": "40817"}}}}}}'
        ),
        llm_service=None,
    )

    parsed = json.loads(result["content"])

    assert result["placeholders"][0]["suggestion"] == "accountOwner.account.number"
    assert (
        parsed["root"]["accountOwner"]["wrapper"]["account"]["details"]["number"]
        == "{{accountOwner.account.number}}"
    )


@pytest.mark.asyncio
async def test_analyze_content_does_not_treat_card_holder_as_card_scope() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [
            {"path": "accountOwner.firstName", "label": "Owner — Имя", "data_type": "string"},
            {
                "path": "accountOwner.card.firstName",
                "label": "Owner card — Имя на карте",
                "data_type": "string",
            },
        ]

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content='{"accountOwner": {"cardHolder": {"firstName": "Иван"}}}',
        llm_service=None,
    )

    parsed = json.loads(result["content"])

    assert result["placeholders"][0]["suggestion"] == "accountOwner.firstName"
    assert parsed["accountOwner"]["cardHolder"]["firstName"] == "{{accountOwner.firstName}}"


def test_path_segments_strip_multiple_index_suffixes() -> None:
    assert TemplateService._path_segments("/root/accountOwner[0][1]/client[2]/firstName") == [
        "root",
        "accountOwner",
        "client",
        "firstName",
    ]


def test_entity_scope_requires_whole_path_segment() -> None:
    assert (
        TemplateService._entity_scope_from_segments(
            ["accountOwner", "creditCard", "firstName"],
            "accountOwner",
        )
        is None
    )
    assert (
        TemplateService._entity_scope_from_segments(
            ["accountOwner", "account", "number"],
            "accountOwner",
        )
        == "account"
    )


@pytest.mark.asyncio
async def test_build_field_catalog_includes_account_owner_paths() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))
    definitions = {
        "client": [SimpleNamespace(name="fullName", label="ФИО", data_type="string")],
        "account": [SimpleNamespace(name="number", label="Номер счёта", data_type="string")],
        "card": [SimpleNamespace(name="number", label="Номер карты", data_type="string")],
    }

    class FakeSchema:
        async def list_schema(self, entity_type: str) -> list[SimpleNamespace]:
            return definitions[entity_type]

    svc.schema = FakeSchema()  # type: ignore[assignment]

    catalog = await svc.build_field_catalog()
    paths = {entry["path"] for entry in catalog}

    assert "accountOwner.fullName" in paths
    assert "accountOwner.account.number" in paths
    assert "accountOwner.card.number" in paths


@pytest.mark.asyncio
async def test_update_placeholders_recalculates_account_owner_meta() -> None:
    class FakeSession:
        flushed = False

        async def flush(self) -> None:
            self.flushed = True

    template_id = uuid.uuid4()
    template = SimpleNamespace(
        id=template_id,
        format="json",
        content='{"ownerName": "Иванов"}',
        original_content='{"ownerName": "Иванов"}',
        placeholders=[],
        llm_meta={"summary": "manual", "has_account_owner": False},
    )
    session = FakeSession()
    svc = TemplateService(cast(Any, session))

    async def fake_get(requested_id: uuid.UUID) -> Any:
        assert requested_id == template_id
        return template

    svc.get = fake_get  # type: ignore[assignment, method-assign]

    updated = await svc.update_placeholders(
        template_id,
        [
            {
                "location": "/ownerName",
                "mode": "mapped",
                "value": "{{accountOwner.ownerName}}",
                "original": "Иванов",
                "suggestion": "accountOwner.ownerName",
            }
        ],
    )

    assert json.loads(updated.content)["ownerName"] == "{{accountOwner.ownerName}}"
    assert updated.llm_meta == {"summary": "manual", "has_account_owner": True}
    assert session.flushed is True
