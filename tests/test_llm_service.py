from __future__ import annotations

import pytest

from app.llm.models import ChatResponse, TokenUsage
from app.llm.service import LLMService


class FakeClient:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.calls: list[tuple[str, str]] = []

    async def chat(self, system_prompt: str, user_prompt: str) -> ChatResponse:
        self.calls.append((system_prompt, user_prompt))
        return ChatResponse(text=self.response_text, token_usage=TokenUsage(1, 2, 3))


@pytest.mark.asyncio
async def test_analyze_template_parses_valid_json() -> None:
    response_text = (
        '{"meta": {"summary": "X", "category": "transfer", "scenarios": ["a"]},'
        ' "placeholders": [{"location": "/fullName", "suggestion": "sender.fullName"}]}'
    )
    client = FakeClient(response_text)
    service = LLMService(client)
    result = await service.analyze_template(
        content='{"fullName":"X"}',
        fmt="json",
        leaves=[{"location": "/fullName", "value": "X"}],
        catalog=[{"path": "sender.fullName", "label": "Sender", "data_type": "string"}],
    )
    assert result["meta"]["category"] == "transfer"
    assert result["placeholders"] == [{"location": "/fullName", "suggestion": "sender.fullName"}]
    assert result["debug"]["system_prompt"]
    assert result["debug"]["user_prompt"]
    assert result["debug"]["response_text"] == response_text


@pytest.mark.asyncio
async def test_analyze_template_handles_garbled_response() -> None:
    # LLM returned non-JSON: service should not raise.
    response_text = "это просто текст, не JSON"
    client = FakeClient(response_text)
    service = LLMService(client)
    result = await service.analyze_template(
        content="{}", fmt="json", leaves=[{"location": "/a", "value": "x"}], catalog=[],
    )
    assert result["placeholders"] == []
    assert result["meta"] == {}
    assert result["debug"]["system_prompt"]
    assert result["debug"]["user_prompt"]
    assert result["debug"]["response_text"] == response_text


@pytest.mark.asyncio
async def test_analyze_template_recovers_json_substring() -> None:
    client = FakeClient('Извините, вот мой ответ: {"meta": {"summary": "S"}, "placeholders": []} спасибо!')
    service = LLMService(client)
    result = await service.analyze_template(
        content="{}", fmt="json", leaves=[{"location": "/a", "value": "x"}], catalog=[],
    )
    assert result["meta"]["summary"] == "S"
