from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from app.llm.coordinator import LLMCoordinator
from app.llm.models import ChatResponse, TokenUsage

log = logging.getLogger(__name__)

# HTTP statuses we should retry on. Anything else is non-retriable.
RETRIABLE_STATUSES = {429, 502, 503, 504}


class GigaChatClient:
    """Async wrapper around the official sync ``gigachat`` SDK."""

    def __init__(
        self,
        *,
        base_url: str,
        cert_file: str,
        key_file: str,
        model: str,
        timeout: float = 120.0,
        max_retries: int = 5,
        retry_base_delay: float = 3.0,
        coordinator: LLMCoordinator | None = None,
    ) -> None:
        from gigachat import GigaChat  # imported lazily so the rest of the app works without the SDK

        self._client = GigaChat(
            base_url=base_url,
            cert_file=cert_file,
            key_file=key_file,
            model=model,
            timeout=timeout,
            verify_ssl_certs=False,
        )
        self._model = model
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._coordinator = coordinator

    async def chat(self, system_prompt: str, user_prompt: str) -> ChatResponse:
        if self._coordinator is not None:
            await self._coordinator.acquire()
        try:
            return await self._chat_with_retries(system_prompt, user_prompt)
        finally:
            if self._coordinator is not None:
                self._coordinator.release()

    async def _chat_with_retries(self, system_prompt: str, user_prompt: str) -> ChatResponse:
        from gigachat.models import Chat, Messages, MessagesRole  # local import for optionality

        payload = Chat(
            messages=[
                Messages(role=MessagesRole.SYSTEM, content=system_prompt),
                Messages(role=MessagesRole.USER, content=user_prompt),
            ],
            stream=False,
            model=self._model,
        )

        attempt = 0
        while True:
            try:
                response = await asyncio.to_thread(self._client.chat, payload)
                return self._parse_response(response)
            except Exception as exc:
                status = self._status_from(exc)
                retry_after = self._retry_after_from(exc)
                if status is not None and status not in RETRIABLE_STATUSES:
                    log.warning("Non-retriable GigaChat error %s: %s", status, exc)
                    raise
                if attempt >= self._max_retries:
                    log.error("GigaChat exhausted retries: %s", exc)
                    raise
                delay = retry_after if retry_after is not None else self._retry_base_delay * (2 ** attempt)
                delay += random.uniform(0, max(0.5, delay * 0.1))  # jitter
                log.warning("GigaChat attempt %d failed (%s) — retrying in %.1fs", attempt + 1, exc, delay)
                await asyncio.sleep(delay)
                attempt += 1

    @staticmethod
    def _status_from(exc: Exception) -> int | None:
        for attr in ("status_code", "status"):
            v = getattr(exc, attr, None)
            if isinstance(v, int):
                return v
        response = getattr(exc, "response", None)
        if response is not None:
            v = getattr(response, "status_code", None)
            if isinstance(v, int):
                return v
        return None

    @staticmethod
    def _retry_after_from(exc: Exception) -> float | None:
        response = getattr(exc, "response", None)
        if response is None:
            return None
        headers = getattr(response, "headers", None) or {}
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    @staticmethod
    def _parse_response(response: Any) -> ChatResponse:
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError("GigaChat: пустой массив choices в ответе")
        first = choices[0]
        message = getattr(first, "message", None)
        if message is None:
            raise RuntimeError("GigaChat: отсутствует message в ответе")
        text = getattr(message, "content", None)
        if not isinstance(text, str):
            raise RuntimeError("GigaChat: content не является строкой")
        usage = getattr(response, "usage", None)
        token_usage = TokenUsage()
        if usage is not None:
            token_usage = TokenUsage(
                prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
            )
        return ChatResponse(text=text, token_usage=token_usage)

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            try:
                await asyncio.to_thread(close)
            except Exception:
                pass

    async def __aenter__(self) -> GigaChatClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
