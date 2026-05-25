from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.llm.coordinator import LLMCoordinator
from app.llm.models import ChatResponse, LLMClient
from app.llm.prompts import PromptBuilder

log = logging.getLogger(__name__)


class LLMService:
    """Higher-level orchestration of LLM tasks for the application."""

    def __init__(self, client: LLMClient, *, coordinator: LLMCoordinator | None = None) -> None:
        self.client = client
        self.coordinator = coordinator
        self.prompts = PromptBuilder()

    async def analyze_template(
        self,
        *,
        content: str,
        fmt: str,
        leaves: list[str],
        catalog: list[dict[str, str]],
    ) -> dict[str, Any]:
        if not leaves:
            return {"placeholders": [], "meta": {"summary": "Пустой шаблон"}}
        system_prompt, user_prompt = self.prompts.build_template_field_mapping(
            content=content, fmt=fmt, leaves=leaves, catalog=catalog
        )
        response: ChatResponse = await self.client.chat(system_prompt, user_prompt)
        debug = {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response_text": response.text,
        }
        parsed = self._parse_json(response.text)
        if not isinstance(parsed, dict):
            log.warning("LLM analyze_template returned non-dict; using empty result")
            return {"placeholders": [], "meta": {}, "debug": debug}
        placeholders = parsed.get("placeholders") or []
        meta = parsed.get("meta") or {}
        normalized = []
        for item in placeholders:
            if not isinstance(item, dict):
                continue
            loc = item.get("location")
            sug = item.get("suggestion") or None
            if loc:
                normalized.append({"location": loc, "suggestion": sug})
        return {"placeholders": normalized, "meta": meta, "debug": debug}

    async def regenerate_meta(self, *, content: str, fmt: str) -> dict[str, Any]:
        system_prompt, user_prompt = self.prompts.build_template_meta(content=content, fmt=fmt)
        response = await self.client.chat(system_prompt, user_prompt)
        parsed = self._parse_json(response.text)
        if isinstance(parsed, dict):
            return parsed
        return {"summary": response.text[:300]}

    @staticmethod
    def _parse_json(text: str) -> Any:
        candidates: list[str] = []
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
        if stripped:
            candidates.append(stripped)
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end != -1 and end > start:
            fragment = stripped[start : end + 1]
            if fragment not in candidates:
                candidates.append(fragment)

        for raw in candidates:
            for candidate in (raw, LLMService._strip_trailing_commas(raw)):
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    continue
        return None

    @staticmethod
    def _strip_trailing_commas(text: str) -> str:
        """Remove trailing commas before ``}`` / ``]`` — a common LLM JSON mistake."""

        prev = None
        out = text
        while prev != out:
            prev = out
            out = re.sub(r",(\s*[}\]])", r"\1", out)
        return out
