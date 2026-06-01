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
    assert result["debug"]["response_text"] == mapping_text


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
    assert result["debug"]["response_text"] == "это просто текст, не JSON"


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
