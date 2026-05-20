from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class ChatResponse:
    text: str
    token_usage: TokenUsage = field(default_factory=TokenUsage)


class LLMClient(Protocol):
    async def chat(self, system_prompt: str, user_prompt: str) -> ChatResponse:
        ...
