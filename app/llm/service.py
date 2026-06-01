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
        leaves: list[dict[str, str]],
        catalog: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Map template leaves to catalog fields and describe the template.

        Runs two focused LLM calls instead of one combined task — a weak model
        copes far better when each prompt asks for one thing: (1) field mapping,
        (2) ``meta`` (summary/category/scenarios). ``leaves`` carry both
        ``location`` and ``value`` so the whole template no longer has to be
        shipped as a JSON blob.
        """

        meta, meta_debug = await self._describe_template(content=content, fmt=fmt)
        if not leaves:
            # The meta call still hit the LLM even without mappable leaves
            # (e.g. envelope-only templates) — surface its debug so ``llm_used``
            # reflects reality and the prompt/response stay diagnosable.
            return {"placeholders": [], "meta": meta, "debug": meta_debug}

        system_prompt, user_prompt, id_to_location = (
            self.prompts.build_template_field_mapping(leaves=leaves, catalog=catalog)
        )
        response: ChatResponse = await self.client.chat(system_prompt, user_prompt)
        debug = self._merge_debug(
            meta=meta_debug,
            mapping={
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "response_text": response.text,
            },
        )
        parsed = self._parse_json(response.text)
        if not isinstance(parsed, dict):
            log.warning(
                "LLM analyze_template returned non-dict; using empty result. "
                "response_text[:200]=%r",
                response.text[:200],
            )
            return {"placeholders": [], "meta": meta, "debug": debug}
        placeholders = parsed.get("placeholders") or []
        # Build a value→location index so a model that echoes the raw path (or a
        # different leaf id) instead of the assigned id still resolves.
        known_locations = set(id_to_location.values())
        normalized = []
        for item in placeholders:
            if not isinstance(item, dict):
                continue
            field = item.get("field") or item.get("suggestion") or None
            ref = item.get("leaf") or item.get("location")
            if not isinstance(ref, str) or not ref:
                continue
            location = id_to_location.get(ref)
            if location is None and ref in known_locations:
                location = ref  # model returned the path itself — accept it
            if location:
                normalized.append({"location": location, "suggestion": field})
        return {"placeholders": normalized, "meta": meta, "debug": debug}

    async def regenerate_meta(self, *, content: str, fmt: str) -> dict[str, Any]:
        meta, _ = await self._describe_template(content=content, fmt=fmt)
        return meta

    async def _describe_template(
        self, *, content: str, fmt: str
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Run the meta (summary/category/scenarios) LLM call.

        Returns ``(meta, debug)`` so callers can both use the description and
        surface the prompt/response — the meta call hits the LLM just like the
        mapping call and must be diagnosable from the debug panel.
        """

        system_prompt, user_prompt = self.prompts.build_template_meta(content=content, fmt=fmt)
        response = await self.client.chat(system_prompt, user_prompt)
        debug = {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response_text": response.text,
        }
        parsed = self._parse_json(response.text)
        meta = parsed if isinstance(parsed, dict) else {"summary": response.text[:300]}
        return meta, debug

    @staticmethod
    def _merge_debug(*, meta: dict[str, str], mapping: dict[str, str]) -> dict[str, str]:
        """Combine the meta and mapping debug into the flat shape the debug
        panel renders, with a labelled section per LLM call."""

        keys = ("system_prompt", "user_prompt", "response_text")
        return {
            key: (
                f"### Описание шаблона (meta)\n{meta.get(key, '')}\n\n"
                f"### Разметка полей (placeholders)\n{mapping.get(key, '')}"
            )
            for key in keys
        }

    @staticmethod
    def _parse_json(text: str) -> Any:
        """Parse a JSON object from an LLM response.

        GigaChat regularly wraps its answer in a markdown code fence
        (``` ```json ... ``` ```) and occasionally emits trailing commas before
        ``}``/``]``. Both make ``json.loads`` fail outright; without recovery
        the caller drops *all* placeholders and meta. This tries multiple
        repair strategies before giving up.
        """

        candidates: list[str] = []
        raw = text.strip()
        fence_stripped = LLMService._strip_code_fence(raw)
        if fence_stripped:
            candidates.append(fence_stripped)
        if raw and raw not in candidates:
            candidates.append(raw)
        # As a last resort, take the widest brace-delimited substring — this
        # handles models that wrap the JSON in prose ("Извините, вот ответ: {…}").
        haystack = fence_stripped or raw
        start = haystack.find("{")
        end = haystack.rfind("}")
        if start != -1 and end != -1 and end > start:
            fragment = haystack[start : end + 1]
            if fragment not in candidates:
                candidates.append(fragment)

        for candidate in candidates:
            for variant in (candidate, LLMService._strip_trailing_commas(candidate)):
                try:
                    return json.loads(variant)
                except json.JSONDecodeError:
                    continue
        return None

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        """Strip a leading ``` / ```json fence and matching trailing ``` if present.

        Tolerates a missing closing fence (truncated responses) — the opening
        line is still removed so the JSON body becomes parseable.
        """

        if not text.startswith("```"):
            return ""
        lines = text.splitlines()
        # First line is the opening fence (``` or ```json or ```JSON ...).
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()

    @staticmethod
    def _strip_trailing_commas(text: str) -> str:
        """Remove trailing commas before ``}``/``]`` — a common LLM JSON mistake."""

        prev = None
        out = text
        while prev != out:
            prev = out
            out = re.sub(r",(\s*[}\]])", r"\1", out)
        return out
