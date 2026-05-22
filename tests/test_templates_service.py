from __future__ import annotations

import json
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
from app.utils.errors import ValidationFailed


def test_normalize_placeholders_accepts_valid_entries() -> None:
    raw = [
        {"location": "/a", "mode": "mapped", "value": "{{sender.x}}", "original": "X"},
        {"location": "/b", "mode": "literal", "value": "lit"},  # original defaults to ""
    ]
    out = normalize_placeholders(raw)
    assert out[0]["location"] == "/a"
    assert out[1]["original"] == ""
    assert out[1]["mode"] == "literal"


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
        original_content='{"operuid": "service-id"}',
        llm_service=None,
    )

    assert json.loads(result["content"])["operuid"] == "service-id"
    assert result["placeholders"][0]["mode"] == "literal"
    assert result["placeholders"][0]["suggestion"] is None


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
async def test_build_field_catalog_includes_account_owner_paths() -> None:
    svc = TemplateService(cast(Any, SimpleNamespace()))
    definitions = {
        "client": [SimpleNamespace(name="fullName", label="ФИО", data_type="string")],
        "account": [SimpleNamespace(name="number", label="Номер счёта", data_type="string")],
        "card": [SimpleNamespace(name="number", label="Номер карты", data_type="string")],
    }

    class FakeSchema:
        async def list_schema(
            self,
            entity_type: str,
            *,
            include_deprecated: bool = False,
        ) -> list[SimpleNamespace]:
            assert include_deprecated is False
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
