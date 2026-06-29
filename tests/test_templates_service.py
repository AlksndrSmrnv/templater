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


@pytest.mark.asyncio
async def test_analyze_content_without_llm_leaves_fields_unmapped() -> None:
    # Without an LLM there is no field mapping anymore (the heuristic fallback was
    # removed): everything stays literal and the preview dict never mutates the model.
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [{"path": "sender.fullName", "label": "Sender — ФИО", "data_type": "string"}]

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content='{"fullName": "Иванов", "note": "x"}',
        llm_service=None,
    )

    assert json.loads(result["content"])["fullName"] == "Иванов"
    assert result["placeholders"][0]["location"] == "/fullName"
    assert result["placeholders"][0]["mode"] == "literal"
    assert result["placeholders"][0]["suggestion"] is None
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
async def test_update_placeholders_clears_stale_llm_debug() -> None:
    # A manual editor save must drop the previously captured LLM debug — the
    # saved placeholders may have been hand-edited, so the old prompts/response
    # no longer describe the saved state.
    class FakeSession:
        async def flush(self) -> None:
            return None

    svc = TemplateService(cast(Any, FakeSession()))
    template = SimpleNamespace(
        format="json",
        content='{"a":"x"}',
        original_content='{"a":"x"}',
        placeholders=[],
        llm_meta={"summary": "ok"},
        llm_debug={"system_prompt": "sys", "user_prompt": "u", "response_text": "r"},
    )

    async def fake_get(tid: Any) -> Any:
        return template

    svc.get = fake_get  # type: ignore[method-assign, assignment]

    await svc.update_placeholders(cast(Any, None), [])

    assert template.llm_debug is None


@pytest.mark.asyncio
async def test_analyze_and_persist_marks_pending_review_awaiting_confirmation() -> None:
    # The LLM result is persisted but NOT promoted to "processed" — the user must
    # review the field mapping and save it (update_placeholders) to unlock fill.
    class FakeSession:
        async def flush(self) -> None:
            return None

    svc = TemplateService(cast(Any, FakeSession()))

    async def fake_analyze_content(
        *, fmt: str, original_content: str, llm_service: Any | None = None
    ) -> dict[str, Any]:
        return {
            "content": original_content,
            "placeholders": [],
            "llm_meta": {"summary": "ok"},
            "llm_debug": None,
        }

    svc.analyze_content = fake_analyze_content  # type: ignore[method-assign]

    template = SimpleNamespace(
        format="json",
        content='{"a":"x"}',
        original_content='{"a":"x"}',
        placeholders=[],
        llm_meta={},
        llm_debug=None,
        headers=[],
    )

    await svc.analyze_and_persist(cast(Any, template), llm_service=object())

    assert template.llm_meta["import_status"] == "pending_review"
    assert template.llm_meta["summary"] == "ok"


@pytest.mark.asyncio
async def test_update_placeholders_confirms_pending_review_to_processed() -> None:
    # Saving the placeholder mapping is the user's sign-off: a pending_review
    # template (LLM ran, mapping not yet confirmed) is promoted to "processed",
    # which unlocks the "Заполнить" button.
    class FakeSession:
        async def flush(self) -> None:
            return None

    template_id = uuid.uuid4()
    template = SimpleNamespace(
        id=template_id,
        format="json",
        content='{"a":"{{sender.fullName}}"}',
        original_content='{"a":"Иванов"}',
        placeholders=[],
        llm_meta={"summary": "ok", "import_status": "pending_review"},
    )
    svc = TemplateService(cast(Any, FakeSession()))

    async def fake_get(requested_id: uuid.UUID) -> Any:
        assert requested_id == template_id
        return template

    svc.get = fake_get  # type: ignore[assignment, method-assign]

    updated = await svc.update_placeholders(
        template_id,
        [
            {
                "location": "/a",
                "mode": "mapped",
                "value": "{{sender.fullName}}",
                "original": "Иванов",
                "suggestion": "sender.fullName",
            }
        ],
    )

    assert updated.llm_meta["import_status"] == "processed"
    assert updated.llm_meta["summary"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing_status", "expected_status"),
    [
        ("processed", "processed"),
        ("imported", "imported"),
        ("unparsed", "unparsed"),
    ],
)
async def test_update_placeholders_leaves_non_pending_statuses_untouched(
    existing_status: str, expected_status: str
) -> None:
    # Only the pending_review → processed confirmation flip happens on save.
    # A routine edit of an already-processed template keeps it processed, and
    # templates without a confirmed LLM run are NOT smuggled to "processed".
    class FakeSession:
        async def flush(self) -> None:
            return None

    template_id = uuid.uuid4()
    template = SimpleNamespace(
        id=template_id,
        format="json",
        content='{"a":"x"}',
        original_content='{"a":"x"}',
        placeholders=[],
        llm_meta={"import_status": existing_status},
    )
    svc = TemplateService(cast(Any, FakeSession()))

    async def fake_get(requested_id: uuid.UUID) -> Any:
        return template

    svc.get = fake_get  # type: ignore[assignment, method-assign]

    updated = await svc.update_placeholders(template_id, [])
    assert updated.llm_meta["import_status"] == expected_status


@pytest.mark.asyncio
async def test_update_placeholders_does_not_add_import_status_when_absent() -> None:
    # A template that never had import_status (e.g. hand-created without LLM)
    # must not gain one on a manual save — preserves the pre-existing behaviour.
    class FakeSession:
        async def flush(self) -> None:
            return None

    template_id = uuid.uuid4()
    template = SimpleNamespace(
        id=template_id,
        format="json",
        content='{"a":"x"}',
        original_content='{"a":"x"}',
        placeholders=[],
        llm_meta={"summary": "manual"},
    )
    svc = TemplateService(cast(Any, FakeSession()))

    async def fake_get(requested_id: uuid.UUID) -> Any:
        return template

    svc.get = fake_get  # type: ignore[assignment, method-assign]

    updated = await svc.update_placeholders(template_id, [])
    assert "import_status" not in updated.llm_meta
    assert updated.llm_meta == {"summary": "manual", "has_account_owner": False}


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
                    {"location": "/ownerName", "suggestion": "accountOwner.ownerName"}
                ],
            }

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content='{"ownerName": "Иванов"}',
        llm_service=FakeLlm(),
    )

    assert json.loads(result["content"])["ownerName"] == "{{accountOwner.ownerName}}"
    assert result["placeholders"][0]["suggestion"] == "accountOwner.ownerName"
    assert result["llm_meta"]["has_account_owner"] is True


@pytest.mark.asyncio
async def test_analyze_content_sets_account_owner_meta_false_without_placeholder() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [{"path": "sender.fullName", "label": "Sender — ФИО", "data_type": "string"}]

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
                "placeholders": [{"location": "/fullName", "suggestion": "sender.fullName"}],
            }

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content='{"fullName": "Иванов"}',
        llm_service=FakeLlm(),
    )

    assert result["placeholders"][0]["suggestion"] == "sender.fullName"
    assert result["llm_meta"]["has_account_owner"] is False


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
async def test_analyze_content_maps_account_owner_field_by_path_role_over_llm_role() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))

    async def fake_catalog() -> list[dict[str, str]]:
        return [
            {"path": "sender.firstName", "label": "Sender — Имя", "data_type": "string"},
            {"path": "accountOwner.firstName", "label": "Owner — Имя", "data_type": "string"},
        ]

    leaf = "/outer/accountOwner/client/unknownEnvelope/firstName"

    class FakeLlm:
        async def analyze_template(
            self,
            *,
            content: str,
            fmt: str,
            leaves: list[dict[str, str]],
            catalog: list[dict[str, str]],
        ) -> dict[str, Any]:
            # LLM guessed the wrong role; the role from the leaf path must win.
            return {
                "meta": {"summary": "llm"},
                "placeholders": [{"location": leaf, "suggestion": "sender.firstName"}],
            }

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content=(
            '{"outer": {"accountOwner": {"client": {"unknownEnvelope": {"firstName": "Иван"}}}}}'
        ),
        llm_service=FakeLlm(),
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

    leaf = "/root/accountOwner/wrapper/account/details/number"

    class FakeLlm:
        async def analyze_template(
            self,
            *,
            content: str,
            fmt: str,
            leaves: list[dict[str, str]],
            catalog: list[dict[str, str]],
        ) -> dict[str, Any]:
            # LLM gave the client-level field; the account scope from the path
            # must steer it to the account-scoped catalog entry.
            return {
                "meta": {"summary": "llm"},
                "placeholders": [{"location": leaf, "suggestion": "accountOwner.number"}],
            }

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content=(
            '{"root": {"accountOwner": {"wrapper": {"account": {"details": {"number": "40817"}}}}}}'
        ),
        llm_service=FakeLlm(),
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

    leaf = "/accountOwner/cardHolder/firstName"

    class FakeLlm:
        async def analyze_template(
            self,
            *,
            content: str,
            fmt: str,
            leaves: list[dict[str, str]],
            catalog: list[dict[str, str]],
        ) -> dict[str, Any]:
            # "cardHolder" is not the "card" scope, so the client-level field wins.
            return {
                "meta": {"summary": "llm"},
                "placeholders": [{"location": leaf, "suggestion": "firstName"}],
            }

    svc.build_field_catalog = fake_catalog  # type: ignore[method-assign]

    result = await svc.analyze_content(
        fmt="json",
        original_content='{"accountOwner": {"cardHolder": {"firstName": "Иван"}}}',
        llm_service=FakeLlm(),
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    ["regenerate_meta_and_persist", "regenerate_fields_and_persist"],
)
async def test_granular_reprocess_refuses_unprocessed_template(method_name: str) -> None:
    # The granular reprocess actions must not be able to flip an un-analysed
    # template to import_status="processed" with partial data (stale/direct POST).
    svc = TemplateService(cast(Any, SimpleNamespace()))

    class ExplodingLlm:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"LLM must not be called: {name}")

    template = cast(
        Any,
        SimpleNamespace(
            id=uuid.uuid4(),
            format="json",
            content='{"a": 1}',
            original_content='{"a": 1}',
            placeholders=[],
            llm_meta={"import_status": "imported"},
        ),
    )

    method = getattr(svc, method_name)
    with pytest.raises(ValidationFailed):
        await method(template, llm_service=ExplodingLlm())
    # Guard runs before any state mutation — nothing was flipped to "processed".
    assert template.llm_meta == {"import_status": "imported"}


@pytest.mark.asyncio
async def test_regenerate_meta_only_updates_meta_and_keeps_placeholders() -> None:
    class FakeSession:
        async def flush(self) -> None:
            return None

    svc = TemplateService(cast(Any, FakeSession()))

    class FakeLlm:
        async def regenerate_meta(self, *, content: str, fmt: str) -> dict[str, Any]:
            return {
                "meta": {"summary": "new"},
                "debug": {"system_prompt": "s", "user_prompt": "u", "response_text": "r"},
            }

    placeholders = [
        {"location": "/ownerName", "suggestion": "accountOwner.ownerName", "value": ""}
    ]
    template = cast(
        Any,
        SimpleNamespace(
            id=uuid.uuid4(),
            format="json",
            content='{"x": "{{sender.fullName}}"}',
            original_content='{"x": "Иванов"}',
            placeholders=placeholders,
            llm_meta={"summary": "old", "import_status": "processed"},
            llm_debug=None,
        ),
    )

    out = await svc.regenerate_meta_and_persist(template, llm_service=FakeLlm())

    assert out.llm_meta["summary"] == "new"
    # A meta re-run produces a fresh LLM output awaiting user confirmation —
    # the template is demoted to pending_review until "Сохранить изменения"
    # flips it back to processed (see update_placeholders).
    assert out.llm_meta["import_status"] == "pending_review"
    # account-owner flag recomputed from the existing placeholders
    assert out.llm_meta["has_account_owner"] is True
    # content + placeholders are left untouched by a meta-only reprocess
    assert out.content == '{"x": "{{sender.fullName}}"}'
    assert out.placeholders is placeholders
    assert cast("dict[str, Any]", out.llm_debug)["response_text"] == "r"


# ---------------------------------------------------------------------------
# Projects: every template belongs to exactly one project.
# ---------------------------------------------------------------------------

from pydantic import ValidationError as PydanticSchemaError  # noqa: E402

from app.schemas.template import TemplateCreate, TemplateUpdate  # noqa: E402


class _ProjectAwareSession:
    """Session double: ``get`` resolves only the known project id."""

    def __init__(
        self, known_project_id: uuid.UUID, *, max_display_order: int = -1
    ) -> None:
        self.known_project_id = known_project_id
        self.added: list[Any] = []
        # What the ``next_display_order`` aggregate "finds" in the target
        # folder; -1 mirrors an empty folder (COALESCE(MAX(...), -1)).
        self._max_display_order = max_display_order

    async def get(self, model: type, ident: Any) -> Any:
        if ident == self.known_project_id:
            return SimpleNamespace(id=ident, name="P", color="#112233")
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def execute(self, stmt: Any) -> Any:
        value = self._max_display_order
        return SimpleNamespace(scalar_one=lambda: value)


def test_template_create_schema_requires_project_id() -> None:
    with pytest.raises(PydanticSchemaError):
        TemplateCreate(name="T", format="json", content="{}")  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_create_sets_project_and_rejects_unknown_project() -> None:
    project_id = uuid.uuid4()
    session = _ProjectAwareSession(project_id)
    svc = TemplateService(cast(Any, session))

    template = await svc.create(
        TemplateCreate(name="T", format="json", content="{}", project_id=project_id)
    )
    assert template.project_id == project_id
    assert template.display_order == 0  # first row in an empty folder

    with pytest.raises(ValidationFailed):
        await svc.create(
            TemplateCreate(name="T", format="json", content="{}", project_id=uuid.uuid4())
        )


@pytest.mark.asyncio
async def test_create_appends_after_ordered_siblings() -> None:
    # A folder whose siblings were manually ordered up to 4: the new request
    # must land *after* them (display_order=5), not at the default 0 where it
    # would jump to the top and collide with the existing first item.
    project_id = uuid.uuid4()
    session = _ProjectAwareSession(project_id, max_display_order=4)
    svc = TemplateService(cast(Any, session))

    template = await svc.create(
        TemplateCreate(
            name="T",
            format="json",
            content="{}",
            project_id=project_id,
            folder_path=["Проект"],
        )
    )
    assert template.display_order == 5


@pytest.mark.asyncio
async def test_update_reassigns_project_and_rejects_unknown() -> None:
    project_id = uuid.uuid4()
    session = _ProjectAwareSession(project_id)
    svc = TemplateService(cast(Any, session))
    template = SimpleNamespace(
        format="json",
        content="{}",
        original_content="{}",
        placeholders=[],
        llm_meta={},
        project_id=uuid.uuid4(),
    )

    async def fake_get(tid: Any) -> Any:
        return template

    svc.get = fake_get  # type: ignore[method-assign, assignment]

    await svc.update(cast(Any, None), TemplateUpdate(project_id=project_id))
    assert template.project_id == project_id

    with pytest.raises(ValidationFailed):
        await svc.update(cast(Any, None), TemplateUpdate(project_id=uuid.uuid4()))


# ---------- edit_content (manual in-place body editing) ----------


def _edit_svc(template: Any) -> TemplateService:
    class FakeSession:
        async def flush(self) -> None:
            return None

    svc = TemplateService(cast(Any, FakeSession()))

    async def fake_get(tid: Any) -> Any:
        return template

    svc.get = fake_get  # type: ignore[method-assign, assignment]
    return svc


def _edit_template(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "format": "json",
        "content": '{\n  "name": "{{sender.fullName}}",\n  "amount": "{{amount}}"\n}',
        "original_content": '{\n  "name": "Иванов",\n  "amount": "100"\n}',
        "placeholders": [
            {"location": "/name", "mode": "mapped", "value": "{{sender.fullName}}", "original": "Иванов"},
            {"location": "/amount", "mode": "dynamic", "value": "{{amount}}", "original": "100"},
        ],
        "llm_meta": {"summary": "ok", "import_status": "processed", "has_account_owner": False},
        "llm_debug": {"system_prompt": "s", "user_prompt": "u", "response_text": "r"},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_edit_content_keeps_matching_and_drops_stale_placeholders() -> None:
    template = _edit_template(
        placeholders=[
            {"location": "/name", "mode": "mapped", "value": "{{sender.fullName}}", "original": "Иванов"},
            {"location": "/removed", "mode": "literal", "value": "x", "original": "x"},
            {"location": "/amount", "mode": "dynamic", "value": "{{amount}}", "original": "100"},
        ]
    )
    svc = _edit_svc(template)

    # /removed is gone, /amount leaf was hand-edited to a literal, /name intact
    new_content = '{\n  "name": "{{sender.fullName}}",\n  "amount": "500",\n  "extra": "added"\n}'
    out = await svc.edit_content(cast(Any, None), new_content)

    assert out.content == new_content
    assert [ph["location"] for ph in out.placeholders] == ["/name"]


@pytest.mark.asyncio
async def test_edit_content_rebuilds_original_content_from_kept_originals() -> None:
    template = _edit_template()
    svc = _edit_svc(template)

    new_content = '{\n  "name": "{{sender.fullName}}",\n  "amount": "{{amount}}",\n  "extra": "added"\n}'
    out = await svc.edit_content(cast(Any, None), new_content)

    original = json.loads(out.original_content)
    assert original["name"] == "Иванов"
    assert original["amount"] == "100"
    # a newly added key must survive in both documents
    assert original["extra"] == "added"
    assert json.loads(out.content)["extra"] == "added"


@pytest.mark.asyncio
async def test_edit_content_without_kept_placeholders_keeps_text_verbatim() -> None:
    template = _edit_template(placeholders=[])
    svc = _edit_svc(template)

    new_content = '{"compact":"kept-as-typed"}'
    out = await svc.edit_content(cast(Any, None), new_content)

    assert out.original_content == new_content
    assert out.content == new_content


@pytest.mark.asyncio
async def test_edit_content_invalid_json_carries_line_and_col() -> None:
    from app.utils.errors import ContentParseFailed

    template = _edit_template()
    svc = _edit_svc(template)

    bad = '{\n  "a": 1,\n  "b": ,\n}'
    with pytest.raises(ContentParseFailed) as exc_info:
        await svc.edit_content(cast(Any, None), bad)

    exc = exc_info.value
    try:
        json.loads(bad)
    except json.JSONDecodeError as ref:
        assert exc.line == ref.lineno
        assert exc.col == ref.colno
    assert exc.details  # rendered as "Строка N, позиция M"
    assert exc.status_code == 422


@pytest.mark.asyncio
async def test_edit_content_xml_keeps_text_and_attr_placeholders() -> None:
    template = _edit_template(
        format="xml",
        content='<doc id="{{rqUID}}"><name>{{sender.fullName}}</name></doc>',
        original_content='<doc id="abc"><name>Иванов</name></doc>',
        placeholders=[
            {"location": "/doc/@id", "mode": "dynamic", "value": "{{rqUID}}", "original": "abc"},
            {"location": "/doc/name[0]/#text", "mode": "mapped", "value": "{{sender.fullName}}", "original": "Иванов"},
        ],
    )
    svc = _edit_svc(template)

    new_content = '<doc id="{{rqUID}}"><name>{{sender.fullName}}</name><extra>x</extra></doc>'
    out = await svc.edit_content(cast(Any, None), new_content)

    assert [ph["location"] for ph in out.placeholders] == ["/doc/@id", "/doc/name[0]/#text"]
    rebuilt = walker.walk_xml(out.original_content)
    values = {leaf.location: leaf.value for leaf in rebuilt}
    assert values["/doc/@id"] == "abc"
    assert values["/doc/name[0]/#text"] == "Иванов"
    assert values["/doc/extra[0]/#text"] == "x"


@pytest.mark.asyncio
async def test_edit_content_invalid_xml_reports_one_based_col() -> None:
    from xml.etree import ElementTree as ET

    from app.utils.errors import ContentParseFailed

    template = _edit_template(format="xml", placeholders=[])
    svc = _edit_svc(template)

    bad = "<doc>\n  <broken>\n</doc>"
    with pytest.raises(ContentParseFailed) as exc_info:
        await svc.edit_content(cast(Any, None), bad)

    try:
        ET.fromstring(bad)
    except ET.ParseError as ref:
        assert exc_info.value.line == ref.position[0]
        assert exc_info.value.col == ref.position[1] + 1


@pytest.mark.asyncio
async def test_edit_content_resets_stale_analysis() -> None:
    # The edited body is a different document: llm_debug and the old summary
    # are dropped, "processed" is demoted to "imported" so the assistant won't
    # use stale analysis, and has_account_owner is recomputed.
    template = _edit_template(
        placeholders=[
            {
                "location": "/owner",
                "mode": "mapped",
                "value": "{{accountOwner.fullName}}",
                "original": "Пётр",
            }
        ],
        content='{"owner": "{{accountOwner.fullName}}"}',
        llm_meta={"summary": "ok", "import_status": "processed", "has_account_owner": True},
    )
    svc = _edit_svc(template)

    # the only accountOwner placeholder is edited away
    out = await svc.edit_content(cast(Any, None), '{"owner": "литерал"}')

    assert out.llm_debug is None
    assert out.llm_meta == {"import_status": "imported", "has_account_owner": False}


@pytest.mark.asyncio
async def test_edit_content_repairing_unparsed_body_clears_unparsed_flag() -> None:
    # A successful save just proved the body parses — the tree's "unparsed"
    # warning flag must not survive the repair.
    template = _edit_template(
        content='{"broken": ',
        original_content='{"broken": ',
        placeholders=[],
        llm_meta={"import_status": "unparsed"},
        llm_debug=None,
    )
    svc = _edit_svc(template)

    out = await svc.edit_content(cast(Any, None), '{"fixed": "да"}')

    assert out.llm_meta == {"import_status": "imported", "has_account_owner": False}


@pytest.mark.asyncio
async def test_edit_content_rejects_empty_body() -> None:
    template = _edit_template()
    svc = _edit_svc(template)

    with pytest.raises(ValidationFailed):
        await svc.edit_content(cast(Any, None), "   \n  ")
