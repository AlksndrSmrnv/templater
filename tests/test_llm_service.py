from __future__ import annotations

import pytest

from app.llm.models import ChatResponse, TokenUsage
from app.llm.service import LLMService

# analyze_template now makes TWO calls in order: (1) meta, (2) field mapping.
# FakeClient hands out responses in that order; with a single response it
# returns the same text for every call.


class FakeClient:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def chat(self, system_prompt: str, user_prompt: str) -> ChatResponse:
        idx = len(self.calls)
        self.calls.append((system_prompt, user_prompt))
        text = self.responses[idx] if idx < len(self.responses) else self.responses[-1]
        return ChatResponse(text=text, token_usage=TokenUsage(1, 2, 3))


_LEAF = [{"location": "/fullName", "value": "X"}]
_CATALOG = [{"path": "sender.fullName", "label": "Sender", "data_type": "string"}]


@pytest.mark.asyncio
async def test_analyze_template_parses_valid_json() -> None:
    meta_text = '{"summary": "X", "category": "transfer", "scenarios": ["a"]}'
    mapping_text = '{"placeholders": [{"leaf": "L1", "field": "sender.fullName"}]}'
    client = FakeClient(meta_text, mapping_text)
    service = LLMService(client)
    result = await service.analyze_template(
        content='{"fullName":"X"}', fmt="json", leaves=_LEAF, catalog=_CATALOG,
    )
    assert result["meta"]["category"] == "transfer"
    assert result["placeholders"] == [{"location": "/fullName", "suggestion": "sender.fullName"}]
    assert result["debug"]["system_prompt"]
    assert result["debug"]["user_prompt"]
    # Debug surfaces BOTH calls — meta and mapping prompt/response.
    assert mapping_text in result["debug"]["response_text"]
    assert meta_text in result["debug"]["response_text"]
    assert "meta" in result["debug"]["system_prompt"]


@pytest.mark.asyncio
async def test_analyze_template_handles_garbled_mapping_response() -> None:
    # Mapping LLM returned non-JSON: service must not raise, placeholders empty.
    client = FakeClient("{}", "это просто текст, не JSON")
    service = LLMService(client)
    result = await service.analyze_template(
        content="{}", fmt="json", leaves=[{"location": "/a", "value": "1"}], catalog=[],
    )
    assert result["placeholders"] == []
    assert result["meta"] == {}
    assert result["debug"]["system_prompt"]
    assert "это просто текст, не JSON" in result["debug"]["response_text"]


@pytest.mark.asyncio
async def test_analyze_template_recovers_meta_json_substring() -> None:
    meta_text = 'Извините, вот мой ответ: {"summary": "S"} спасибо!'
    client = FakeClient(meta_text, '{"placeholders": []}')
    service = LLMService(client)
    result = await service.analyze_template(
        content="{}", fmt="json", leaves=[{"location": "/a", "value": "1"}], catalog=[],
    )
    assert result["meta"]["summary"] == "S"


@pytest.mark.asyncio
async def test_analyze_template_strips_markdown_fences() -> None:
    # GigaChat habitually wraps its JSON in a ```json … ``` fence even though
    # the system prompt asks for raw JSON. Parser must tolerate it.
    mapping_text = (
        "```json\n"
        '{"placeholders": [{"leaf": "L1", "field": "sender.fullName"}]}\n'
        "```"
    )
    client = FakeClient('{"summary": "S"}', mapping_text)
    service = LLMService(client)
    result = await service.analyze_template(
        content='{"fullName":"X"}', fmt="json", leaves=_LEAF, catalog=_CATALOG,
    )
    assert result["placeholders"] == [{"location": "/fullName", "suggestion": "sender.fullName"}]


@pytest.mark.asyncio
async def test_analyze_template_parses_trailing_commas() -> None:
    mapping_text = '{"placeholders": [{"leaf": "L1", "field": "sender.fullName",},],}'
    client = FakeClient('{"summary": "S"}', mapping_text)
    service = LLMService(client)
    result = await service.analyze_template(
        content='{"fullName":"X"}', fmt="json", leaves=_LEAF, catalog=_CATALOG,
    )
    assert result["placeholders"] == [{"location": "/fullName", "suggestion": "sender.fullName"}]


@pytest.mark.asyncio
async def test_analyze_template_handles_fence_with_trailing_commas() -> None:
    # Worst-case real response: fenced AND with trailing commas. Also missing
    # closing fence — simulates a response truncated at the token limit.
    mapping_text = (
        "```json\n"
        '{"placeholders": [{"leaf": "L1", "field": "sender.fullName",},],}\n'
        "```"
    )
    client = FakeClient('{"summary": "S"}', mapping_text)
    service = LLMService(client)
    result = await service.analyze_template(
        content='{"fullName":"X"}', fmt="json", leaves=_LEAF, catalog=_CATALOG,
    )
    assert result["placeholders"] == [{"location": "/fullName", "suggestion": "sender.fullName"}]


@pytest.mark.asyncio
async def test_analyze_template_accepts_raw_path_in_leaf_field() -> None:
    # Tolerance: model echoed the path itself instead of the assigned id.
    mapping_text = '{"placeholders": [{"leaf": "/fullName", "field": "sender.fullName"}]}'
    client = FakeClient('{"summary": "S"}', mapping_text)
    service = LLMService(client)
    result = await service.analyze_template(
        content='{"fullName":"X"}', fmt="json", leaves=_LEAF, catalog=_CATALOG,
    )
    assert result["placeholders"] == [{"location": "/fullName", "suggestion": "sender.fullName"}]


@pytest.mark.asyncio
async def test_analyze_template_without_leaves_skips_mapping_call() -> None:
    client = FakeClient('{"summary": "пусто"}')
    service = LLMService(client)
    result = await service.analyze_template(
        content="{}", fmt="json", leaves=[], catalog=_CATALOG,
    )
    assert result["placeholders"] == []
    assert result["meta"]["summary"] == "пусто"
    # Only the meta call happened — no mapping call.
    assert len(client.calls) == 1
    # The meta call still hit the LLM, so its debug must be surfaced (otherwise
    # llm_used would wrongly report False and the prompt couldn't be diagnosed).
    assert result["debug"]["response_text"] == '{"summary": "пусто"}'
    assert result["debug"]["system_prompt"]


# --- Transfer assistant pick methods ---

_TEMPLATES = [{"id": "T1", "category": "перевод", "summary": "со счёта на счёт"}]
_CLIENTS = [{"id": "C1", "traits": "резидент", "description": "Иванов"}]
_ACCOUNTS = [{"id": "A1", "client": "C1", "currency": "USD", "description": "счёт"}]
_CARDS = [{"id": "K1", "account": "A1", "description": "карта"}]


@pytest.mark.asyncio
async def test_pick_transfer_template_returns_short_id() -> None:
    service = LLMService(FakeClient('{"template": "T1"}'))
    assert await service.pick_transfer_template(request="x", templates=_TEMPLATES) == "T1"


@pytest.mark.asyncio
async def test_pick_transfer_template_handles_fence_and_null() -> None:
    fenced = LLMService(FakeClient('```json\n{"template": "T1"}\n```'))
    assert await fenced.pick_transfer_template(request="x", templates=_TEMPLATES) == "T1"
    none = LLMService(FakeClient('{"template": null}'))
    assert await none.pick_transfer_template(request="x", templates=_TEMPLATES) is None
    garbage = LLMService(FakeClient("извините, не знаю"))
    assert await garbage.pick_transfer_template(request="x", templates=_TEMPLATES) is None


@pytest.mark.asyncio
async def test_pick_transfer_participants_normalizes_roles() -> None:
    text = (
        '{"sender": {"client": "C1", "account": "A1", "card": null},'
        ' "receiver": {"client": "C2"}}'
    )
    service = LLMService(FakeClient(text))
    picks = await service.pick_transfer_participants(
        request="x", clients=_CLIENTS, accounts=_ACCOUNTS, cards=_CARDS,
        need_account_owner=False,
    )
    assert picks == {
        "sender": {"client": "C1", "account": "A1", "card": None},
        "receiver": {"client": "C2", "account": None, "card": None},
    }


@pytest.mark.asyncio
async def test_pick_transfer_participants_drops_roles_without_client() -> None:
    # A role with no usable client short-id is dropped entirely.
    text = '{"sender": {"client": "C1"}, "receiver": {"account": "A9"}}'
    service = LLMService(FakeClient(text))
    picks = await service.pick_transfer_participants(
        request="x", clients=_CLIENTS, accounts=_ACCOUNTS, cards=_CARDS,
        need_account_owner=False,
    )
    assert picks == {"sender": {"client": "C1", "account": None, "card": None}}


@pytest.mark.asyncio
async def test_pick_transfer_participants_tolerates_garbage() -> None:
    service = LLMService(FakeClient("не JSON вовсе"))
    picks = await service.pick_transfer_participants(
        request="x", clients=_CLIENTS, accounts=_ACCOUNTS, cards=_CARDS,
        need_account_owner=True,
    )
    assert picks == {}
