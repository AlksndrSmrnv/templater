from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.llm.coordinator import LLMCoordinator
from app.llm.models import ChatResponse, LLMClient
from app.llm.prompts import PromptBuilder

log = logging.getLogger(__name__)

# Matches a single flat ``{...}`` object (no nested braces) — used to salvage the
# placeholder objects a model finished writing before a truncated response cut it
# off. Placeholder objects are flat (``{"leaf":"L1","field":"x"}``), so the outer
# ``{"placeholders":[...]}`` wrapper never matches.
_PLACEHOLDER_OBJECT_RE = re.compile(r"\{[^{}]*\}")


class LLMService:
    """Higher-level orchestration of LLM tasks for the application."""

    def __init__(
        self,
        client: LLMClient,
        *,
        coordinator: LLMCoordinator | None = None,
        prompt_overrides: dict[str, str] | None = None,
        field_mapping_max_attempts: int = 2,
    ) -> None:
        self.client = client
        self.coordinator = coordinator
        self.prompts = PromptBuilder(prompt_overrides)
        # Max LLM calls for one field-mapping run. A weak model sometimes cuts the
        # answer off, leaving leaves unmapped; we re-ask for only those leaves and
        # merge. 2 means "first call + at most one retry".
        self._field_mapping_max_attempts = max(1, field_mapping_max_attempts)

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
        """Run the field-mapping LLM call (no meta description), retrying once for
        leaves the model failed to map.

        A weak model sometimes doesn't finish the answer: it truncates the JSON,
        leaves ``field`` empty, or confuses a leaf id with a path. When such a
        failure signal is seen we re-ask the model for *only* the still-unmapped
        leaves and merge the result with what already resolved. Every attempt's
        prompt/response is kept so the debug panel shows that a retry happened.

        Returns ``{"placeholders": [...], "debug": {...}}``. Split out of
        :meth:`analyze_template` so the "reprocess only the template" action can
        re-run the mapping without touching the stored meta/summary.
        """

        if not leaves:
            return {"placeholders": [], "debug": {}}

        remaining = list(leaves)
        resolved: dict[str, str] = {}
        attempts: list[dict[str, str]] = []
        max_attempts = self._field_mapping_max_attempts

        for attempt_no in range(1, max_attempts + 1):
            if not remaining:
                break
            system_prompt, user_prompt, id_to_location = (
                self.prompts.build_template_field_mapping(leaves=remaining, catalog=catalog)
            )
            response: ChatResponse = await self.client.chat(system_prompt, user_prompt)
            attempts.append(
                {
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                    "response_text": response.text,
                }
            )
            newly, retry_needed = self._collect_mappings(response.text, id_to_location)
            resolved.update(newly)
            remaining = [
                leaf for leaf in remaining if leaf.get("location") not in resolved
            ]
            if retry_needed and remaining and attempt_no < max_attempts:
                log.info(
                    "LLM field mapping attempt %d showed failure signals — "
                    "retrying %d unmapped leaf(es)",
                    attempt_no,
                    len(remaining),
                )
                continue
            break

        placeholders = [
            {"location": location, "suggestion": field}
            for location, field in resolved.items()
        ]
        return {"placeholders": placeholders, "debug": self._compose_field_debug(attempts)}

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
    def _collect_mappings(
        text: str, id_to_location: dict[str, str]
    ) -> tuple[dict[str, str], bool]:
        """Resolve one field-mapping response into ``{location: field}`` and flag
        whether a retry is warranted.

        ``retry_needed`` is set on explicit failure evidence only:
          • a truncated / unparseable response (see :meth:`_parse_field_mapping`);
          • a returned leaf whose ``field`` is empty/missing (the model started
            the entry but didn't finish it);
          • a ``leaf``/``location`` ref that resolves to nothing (the model put a
            path where the short id was expected, and it didn't match).

        Leaves the model simply omitted do NOT by themselves request a retry — an
        omission is indistinguishable from a deliberate "no matching field" skip.
        But once ``retry_needed`` is set, the caller re-runs *all* still-unmapped
        leaves, which also recovers a truncated tail.
        """

        items, truncated = LLMService._parse_field_mapping(text)
        retry_needed = truncated
        known_locations = set(id_to_location.values())
        resolved: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            ref = item.get("leaf") or item.get("location")
            if not isinstance(ref, str) or not ref:
                continue
            location = id_to_location.get(ref)
            if location is None and ref in known_locations:
                location = ref  # model returned the path itself — accept it
            if location is None:
                retry_needed = True  # symptom 1: ref didn't resolve to a leaf
                continue
            field = item.get("field") or item.get("suggestion")
            if not isinstance(field, str) or not field.strip():
                retry_needed = True  # symptom 2: model left the field empty
                continue
            resolved[location] = field
        return resolved, retry_needed

    @staticmethod
    def _parse_field_mapping(text: str) -> tuple[list[Any], bool]:
        """Return ``(items, truncated)`` for a field-mapping response.

        On a clean parse, ``items`` is the ``placeholders`` list and
        ``truncated`` is ``False``. When the strict parse can't yield a dict at
        all — GigaChat cut the answer off mid-array (the "doesn't finish the
        answer" symptom) — salvage every complete ``{...}`` object from the
        (possibly unterminated) text so leaves emitted before the cut-off are
        kept, and flag ``truncated=True`` so the caller retries the rest.
        """

        parsed = LLMService._parse_json(text)
        if isinstance(parsed, dict):
            placeholders = parsed.get("placeholders")
            if isinstance(placeholders, list):
                return placeholders, False
            # Complete JSON, just no usable placeholders list — not a truncation.
            return [], False
        return LLMService._salvage_placeholder_items(text), True

    @staticmethod
    def _salvage_placeholder_items(text: str) -> list[dict[str, Any]]:
        """Extract complete ``{...}`` objects from a (possibly truncated) response.

        Used when the strict JSON parse fails — recovers the placeholder objects
        the model finished writing before it was cut off. Each braced fragment is
        parsed individually (tolerating a trailing comma); unparseable fragments
        are skipped.
        """

        items: list[dict[str, Any]] = []
        for match in _PLACEHOLDER_OBJECT_RE.finditer(text):
            fragment = LLMService._strip_trailing_commas(match.group(0))
            try:
                obj = json.loads(fragment)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                items.append(obj)
        return items

    @staticmethod
    def _compose_field_debug(attempts: list[dict[str, str]]) -> dict[str, str]:
        """Flatten per-attempt prompt/response into the 3-key debug shape the
        panel renders.

        A single attempt keeps the plain shape (no headers). Multiple attempts
        are concatenated under ``### Попытка N`` headers within each key, so a
        retry is plainly visible under the "Показать запрос и ответ LLM" button.
        """

        if not attempts:
            return {}
        if len(attempts) == 1:
            return dict(attempts[0])
        keys = ("system_prompt", "user_prompt", "response_text")
        out: dict[str, str] = {}
        for key in keys:
            sections = []
            for index, attempt in enumerate(attempts, start=1):
                header = (
                    "### Попытка 1"
                    if index == 1
                    else f"### Попытка {index} — повтор по неразмеченным листьям"
                )
                sections.append(f"{header}\n{attempt.get(key, '')}")
            out[key] = "\n\n".join(sections)
        return out

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
