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


class FlakyClient:
    """Hands out queued responses but raises on a designated call index — used to
    check that a failing retry doesn't discard an earlier successful attempt."""

    def __init__(self, *responses: str, raise_on: int) -> None:
        self.responses = list(responses)
        self.raise_on = raise_on  # 0-based index of the call that raises
        self.calls: list[tuple[str, str]] = []

    async def chat(self, system_prompt: str, user_prompt: str) -> ChatResponse:
        idx = len(self.calls)
        self.calls.append((system_prompt, user_prompt))
        if idx == self.raise_on:
            raise RuntimeError("LLM down")
        return ChatResponse(text=self.responses[idx], token_usage=TokenUsage(1, 2, 3))


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


# --- Field-mapping retry for leaves a weak model fails to map ---

_MAP_CATALOG = [
    {"path": "sender.a", "label": "A"},
    {"path": "sender.b", "label": "B"},
    {"path": "sender.c", "label": "C"},
]


@pytest.mark.asyncio
async def test_map_template_fields_retries_empty_field_and_merges() -> None:
    # Attempt 1 maps L1 but leaves L2's field empty (model cut off mid-answer);
    # the retry re-asks only the still-unmapped leaf (renumbered to L1) and the
    # two results are merged.
    leaves = [{"location": "/a", "value": "1"}, {"location": "/b", "value": "2"}]
    attempt1 = '{"placeholders":[{"leaf":"L1","field":"sender.a"},{"leaf":"L2","field":""}]}'
    attempt2 = '{"placeholders":[{"leaf":"L1","field":"sender.b"}]}'
    service = LLMService(FakeClient(attempt1, attempt2))
    result = await service.map_template_fields(leaves=leaves, catalog=_MAP_CATALOG)

    assert {"location": "/a", "suggestion": "sender.a"} in result["placeholders"]
    assert {"location": "/b", "suggestion": "sender.b"} in result["placeholders"]
    assert len(result["placeholders"]) == 2
    # Exactly one retry happened, and both attempts are visible in the debug.
    debug = result["debug"]
    assert "### Попытка 1" in debug["response_text"]
    assert "### Попытка 2" in debug["response_text"]
    assert attempt1 in debug["response_text"]
    assert attempt2 in debug["response_text"]


@pytest.mark.asyncio
async def test_map_template_fields_retries_unresolved_ref() -> None:
    # Symptom 1: model put a path where the short id was expected, and it matches
    # no known leaf → that leaf is unmapped → retry.
    leaves = [{"location": "/a", "value": "1"}]
    bad = '{"placeholders":[{"leaf":"/unknown/path","field":"sender.a"}]}'
    good = '{"placeholders":[{"leaf":"L1","field":"sender.a"}]}'
    client = FakeClient(bad, good)
    service = LLMService(client)
    result = await service.map_template_fields(leaves=leaves, catalog=_MAP_CATALOG)

    assert result["placeholders"] == [{"location": "/a", "suggestion": "sender.a"}]
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_map_template_fields_salvages_truncated_array() -> None:
    # Response cut off mid-array: the two complete objects are salvaged from
    # attempt 1, and only the missing tail leaf is retried.
    leaves = [
        {"location": "/a", "value": "1"},
        {"location": "/b", "value": "2"},
        {"location": "/c", "value": "3"},
    ]
    truncated = (
        '{"placeholders":[{"leaf":"L1","field":"sender.a"},'
        '{"leaf":"L2","field":"sender.b"},{"leaf":"L3"'
    )
    rest = '{"placeholders":[{"leaf":"L1","field":"sender.c"}]}'
    client = FakeClient(truncated, rest)
    service = LLMService(client)
    result = await service.map_template_fields(leaves=leaves, catalog=_MAP_CATALOG)

    locations = {p["location"]: p["suggestion"] for p in result["placeholders"]}
    assert locations == {"/a": "sender.a", "/b": "sender.b", "/c": "sender.c"}
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_map_template_fields_clean_response_single_call() -> None:
    # A clean response makes exactly one call and keeps the plain debug shape.
    leaves = [{"location": "/a", "value": "1"}]
    clean = '{"placeholders":[{"leaf":"L1","field":"sender.a"}]}'
    client = FakeClient(clean)
    service = LLMService(client)
    result = await service.map_template_fields(leaves=leaves, catalog=_MAP_CATALOG)

    assert result["placeholders"] == [{"location": "/a", "suggestion": "sender.a"}]
    assert len(client.calls) == 1
    assert "Попытка" not in result["debug"]["response_text"]
    assert result["debug"]["response_text"] == clean


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "{}",
        '{"placeholders": null}',
        '{"placeholders":[{"field":"sender.a"}]}',  # item has no leaf/location
    ],
)
async def test_map_template_fields_retries_wrong_shape(bad: str) -> None:
    # Valid JSON of the wrong shape is a model failure, not a clean "nothing
    # matched" — it must be retried.
    leaves = [{"location": "/a", "value": "1"}]
    good = '{"placeholders":[{"leaf":"L1","field":"sender.a"}]}'
    client = FakeClient(bad, good)
    service = LLMService(client)
    result = await service.map_template_fields(leaves=leaves, catalog=_MAP_CATALOG)

    assert result["placeholders"] == [{"location": "/a", "suggestion": "sender.a"}]
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_map_template_fields_empty_placeholders_is_not_retried() -> None:
    # An explicit empty list is a legitimate "nothing matched" — no retry.
    leaves = [{"location": "/a", "value": "1"}]
    client = FakeClient('{"placeholders": []}')
    service = LLMService(client)
    result = await service.map_template_fields(leaves=leaves, catalog=_MAP_CATALOG)

    assert result["placeholders"] == []
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_map_template_fields_keeps_partial_result_when_retry_errors() -> None:
    # Attempt 1 maps /a but leaves /b empty (→ retry); the retry call raises.
    # The leaf already resolved must survive instead of the whole op failing.
    leaves = [{"location": "/a", "value": "1"}, {"location": "/b", "value": "2"}]
    attempt1 = '{"placeholders":[{"leaf":"L1","field":"sender.a"},{"leaf":"L2","field":""}]}'
    client = FlakyClient(attempt1, raise_on=1)
    service = LLMService(client)
    result = await service.map_template_fields(leaves=leaves, catalog=_MAP_CATALOG)

    assert result["placeholders"] == [{"location": "/a", "suggestion": "sender.a"}]
    assert len(client.calls) == 2  # retry was attempted, then failed gracefully


@pytest.mark.asyncio
async def test_map_template_fields_first_call_error_propagates() -> None:
    # Nothing to preserve on the very first call — the error must surface.
    leaves = [{"location": "/a", "value": "1"}]
    client = FlakyClient(raise_on=0)
    service = LLMService(client)
    with pytest.raises(RuntimeError):
        await service.map_template_fields(leaves=leaves, catalog=_MAP_CATALOG)


@pytest.mark.asyncio
async def test_map_template_fields_caps_attempts() -> None:
    # A persistent failure must not loop forever — it stops at max_attempts.
    leaves = [{"location": "/a", "value": "1"}]
    bad = '{"placeholders":[{"leaf":"L1","field":""}]}'
    client = FakeClient(bad)
    service = LLMService(client, field_mapping_max_attempts=2)
    result = await service.map_template_fields(leaves=leaves, catalog=_MAP_CATALOG)

    assert result["placeholders"] == []
    assert len(client.calls) == 2


# --- Transfer assistant pick methods ---

_TEMPLATES = [{"id": "T1", "category": "перевод", "summary": "со счёта на счёт"}]
_CLIENTS = [{"id": "C1", "traits": "резидент", "description": "Иванов"}]
_ACCOUNTS = [{"id": "A1", "client": "C1", "currency": "USD", "description": "счёт"}]
_CARDS = [{"id": "K1", "account": "A1", "description": "карта"}]


@pytest.mark.asyncio
async def test_pick_transfer_template_returns_short_id() -> None:
    service = LLMService(FakeClient('{"template": "T1"}'))
    chosen, debug = await service.pick_transfer_template(request="x", templates=_TEMPLATES)
    assert chosen == "T1"
    # The prompt/response are surfaced for the assistant's debug panel.
    assert set(debug) == {"system_prompt", "user_prompt", "response_text"}
    assert debug["response_text"] == '{"template": "T1"}'


@pytest.mark.asyncio
async def test_pick_transfer_template_handles_fence_and_null() -> None:
    fenced = LLMService(FakeClient('```json\n{"template": "T1"}\n```'))
    chosen, _ = await fenced.pick_transfer_template(request="x", templates=_TEMPLATES)
    assert chosen == "T1"
    none = LLMService(FakeClient('{"template": null}'))
    chosen, _ = await none.pick_transfer_template(request="x", templates=_TEMPLATES)
    assert chosen is None
    garbage = LLMService(FakeClient("извините, не знаю"))
    chosen, _ = await garbage.pick_transfer_template(request="x", templates=_TEMPLATES)
    assert chosen is None


@pytest.mark.asyncio
async def test_pick_transfer_participants_normalizes_roles() -> None:
    text = (
        '{"sender": {"client": "C1", "account": "A1", "card": null},'
        ' "receiver": {"client": "C2"}}'
    )
    service = LLMService(FakeClient(text))
    picks, debug = await service.pick_transfer_participants(
        request="x", clients=_CLIENTS, accounts=_ACCOUNTS, cards=_CARDS,
        need_account_owner=False,
    )
    assert picks == {
        "sender": {"client": "C1", "account": "A1", "card": None},
        "receiver": {"client": "C2", "account": None, "card": None},
    }
    assert set(debug) == {"system_prompt", "user_prompt", "response_text"}
    assert debug["response_text"] == text


@pytest.mark.asyncio
async def test_pick_transfer_participants_drops_roles_without_client() -> None:
    # A role with no usable client short-id is dropped entirely.
    text = '{"sender": {"client": "C1"}, "receiver": {"account": "A9"}}'
    service = LLMService(FakeClient(text))
    picks, _ = await service.pick_transfer_participants(
        request="x", clients=_CLIENTS, accounts=_ACCOUNTS, cards=_CARDS,
        need_account_owner=False,
    )
    assert picks == {"sender": {"client": "C1", "account": None, "card": None}}


@pytest.mark.asyncio
async def test_pick_transfer_participants_tolerates_garbage() -> None:
    service = LLMService(FakeClient("не JSON вовсе"))
    picks, _ = await service.pick_transfer_participants(
        request="x", clients=_CLIENTS, accounts=_ACCOUNTS, cards=_CARDS,
        need_account_owner=True,
    )
    assert picks == {}
