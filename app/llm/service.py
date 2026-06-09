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

    def __init__(
        self,
        client: LLMClient,
        *,
        coordinator: LLMCoordinator | None = None,
        prompt_overrides: dict[str, str] | None = None,
    ) -> None:
        self.client = client
        self.coordinator = coordinator
        self.prompts = PromptBuilder(prompt_overrides)

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

        mapping = await self.map_template_fields(leaves=leaves, catalog=catalog)
        debug = self._merge_debug(meta=meta_debug, mapping=mapping["debug"])
        return {"placeholders": mapping["placeholders"], "meta": meta, "debug": debug}

    async def map_template_fields(
        self,
        *,
        leaves: list[dict[str, str]],
        catalog: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Run only the field-mapping LLM call (no meta description).

        Returns ``{"placeholders": [...], "debug": {...}}``. Split out of
        :meth:`analyze_template` so the "reprocess only the template" action can
        re-run the mapping without touching the stored meta/summary.
        """

        if not leaves:
            return {"placeholders": [], "debug": {}}

        system_prompt, user_prompt, id_to_location = (
            self.prompts.build_template_field_mapping(leaves=leaves, catalog=catalog)
        )
        response: ChatResponse = await self.client.chat(system_prompt, user_prompt)
        debug = {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response_text": response.text,
        }
        parsed = self._parse_json(response.text)
        if not isinstance(parsed, dict):
            log.warning(
                "LLM map_template_fields returned non-dict; using empty result. "
                "response_text[:200]=%r",
                response.text[:200],
            )
            return {"placeholders": [], "debug": debug}
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
        return {"placeholders": normalized, "debug": debug}

    async def regenerate_meta(self, *, content: str, fmt: str) -> dict[str, Any]:
        """Re-run only the meta (summary) LLM call.

        Returns ``{"meta": ..., "debug": ...}`` so the "reprocess only the
        metadata" action can both persist the new summary and keep the
        prompt/response viewable in the debug panel.
        """

        meta, debug = await self._describe_template(content=content, fmt=fmt)
        return {"meta": meta, "debug": debug}

    async def pick_transfer_template(
        self,
        *,
        request: str,
        templates: list[dict[str, str]],
    ) -> str | None:
        """Ask the LLM to pick ONE template short-id for the transfer request.

        Returns the chosen id (e.g. ``"T2"``) or ``None`` when the model declines
        / returns garbage. Reuses the robust :meth:`_parse_json` recovery so a
        fenced / prose-wrapped answer still resolves.
        """

        system_prompt, user_prompt = self.prompts.build_transfer_template_pick(
            request=request, templates=templates
        )
        response: ChatResponse = await self.client.chat(system_prompt, user_prompt)
        parsed = self._parse_json(response.text)
        if not isinstance(parsed, dict):
            log.warning(
                "LLM pick_transfer_template returned non-dict. response_text[:200]=%r",
                response.text[:200],
            )
            return None
        chosen = parsed.get("template")
        return chosen if isinstance(chosen, str) and chosen else None

    async def pick_transfer_participants(
        self,
        *,
        request: str,
        clients: list[dict[str, str]],
        accounts: list[dict[str, str]],
        cards: list[dict[str, str]],
        need_account_owner: bool,
    ) -> dict[str, dict[str, str | None]]:
        """Ask the LLM to pick participants (client/account/card per role).

        Returns ``{role: {"client": "C1", "account": "A2"|None, "card": None}}``
        for each role the model filled with a usable client. Roles without a
        client are dropped.
        """

        system_prompt, user_prompt = self.prompts.build_transfer_participants(
            request=request,
            clients=clients,
            accounts=accounts,
            cards=cards,
            need_account_owner=need_account_owner,
        )
        response: ChatResponse = await self.client.chat(system_prompt, user_prompt)
        parsed = self._parse_json(response.text)
        if not isinstance(parsed, dict):
            log.warning(
                "LLM pick_transfer_participants returned non-dict. response_text[:200]=%r",
                response.text[:200],
            )
        return self._normalize_participants(parsed)

    @staticmethod
    def _normalize_participants(parsed: Any) -> dict[str, dict[str, str | None]]:
        """Coerce the raw participant JSON into clean per-role short-id picks.

        A role is kept only if it carries a non-empty ``client`` short-id;
        ``account``/``card`` are optional and default to ``None``.
        """

        out: dict[str, dict[str, str | None]] = {}
        if not isinstance(parsed, dict):
            return out
        for role in ("sender", "receiver", "accountOwner"):
            raw = parsed.get(role)
            if not isinstance(raw, dict):
                continue
            client = raw.get("client")
            if not isinstance(client, str) or not client:
                continue
            entry: dict[str, str | None] = {"client": client, "account": None, "card": None}
            for key in ("account", "card"):
                value = raw.get(key)
                if isinstance(value, str) and value:
                    entry[key] = value
            out[role] = entry
        return out

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
