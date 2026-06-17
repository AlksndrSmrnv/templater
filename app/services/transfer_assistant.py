"""LLM-assisted transfer composer for the home page.

Given a user's free-form request ("перевод с карты в долларах на счёт в рублях,
отправитель — резидент, получатель — недееспособный"), this picks a processed
template and the participating clients/accounts/cards via two focused LLM calls,
then renders a ready-to-use filled message.

Two calls (template, then participants) keep each prompt small and single-purpose
— a weak model copes far better than with one combined task, mirroring
:meth:`app.llm.service.LLMService.analyze_template`.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account, Card, Client, MessageTemplate
from app.services.entities import AccountService, CardService, ClientService
from app.services.placeholders import PlaceholderFiller
from app.services.template_render import render_filled_html
from app.services.templates import TemplateService, template_has_account_owner
from app.utils.errors import ValidationFailed

# Human-readable renderings of the seeded enum attributes the model reasons about
# (see alembic 0001_init: client.residency / client.capacity).
_RESIDENCY = {"resident": "резидент", "non_resident": "нерезидент"}
_CAPACITY = {
    "capable": "дееспособный",
    "limited": "ограниченно дееспособный",
    "incapable": "недееспособный",
}

ROLE_TITLES = {
    "sender": "Отправитель",
    "receiver": "Получатель",
    "accountOwner": "Владелец счёта",
}
# role → (client_key, account_key, card_key) in TemplateFillRequest / FILL_KEYS.
ROLE_FILL_KEYS = {
    "sender": ("sender_client_id", "sender_account_id", "sender_card_id"),
    "receiver": ("receiver_client_id", "receiver_account_id", "receiver_card_id"),
    "accountOwner": (
        "account_owner_client_id",
        "account_owner_account_id",
        "account_owner_card_id",
    ),
}


def _client_traits(attrs: dict[str, Any]) -> str:
    parts: list[str] = []
    residency = attrs.get("residency")
    if residency:
        parts.append(_RESIDENCY.get(str(residency), str(residency)))
    capacity = attrs.get("capacity")
    if capacity:
        parts.append(_CAPACITY.get(str(capacity), str(capacity)))
    return ", ".join(parts)


def _with_tags(description: str | None, tags: list[str] | None) -> str:
    text = (description or "").strip()
    clean_tags = [t for t in (tags or []) if t]
    if clean_tags:
        suffix = "теги: " + ", ".join(clean_tags)
        return f"{text} [{suffix}]" if text else f"[{suffix}]"
    return text


def _currency(account: Account) -> str:
    value = (account.attributes or {}).get("currency_id")
    return str(value) if value else ""


class TransferAssistant:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.templates = TemplateService(session)
        self.clients = ClientService(session)
        self.accounts = AccountService(session)
        self.cards = CardService(session)

    async def processed_templates(self) -> list[MessageTemplate]:
        """LLM-processed templates with dynamic fields applied.

        Only these are eligible: the request explicitly requires templates that
        the LLM has analysed (``import_status == "processed"``) and where dynamic
        placeholders were actually filled in.
        """

        out: list[MessageTemplate] = []
        for tpl in await self.templates.list_all():
            meta = tpl.llm_meta or {}
            if meta.get("import_status") != "processed":
                continue
            if not (tpl.placeholders or []):
                continue
            out.append(tpl)
        return out

    async def compose(
        self,
        prompt: str,
        llm_service: Any,
        *,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> dict[str, Any]:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValidationFailed("Введите запрос для подбора перевода")

        templates = await self.processed_templates()
        if not templates:
            raise ValidationFailed(
                "Нет обработанных LLM шаблонов с проставленными динамическими полями. "
                "Сначала обработайте шаблон во вкладке «Шаблоны сообщений»."
            )

        # ---- Call 1: pick the template ----
        template_ids = {f"T{i}": tpl for i, tpl in enumerate(templates, start=1)}
        catalog = [
            {
                "id": short_id,
                "summary": (tpl.llm_meta or {}).get("summary", "") or tpl.description,
            }
            for short_id, tpl in template_ids.items()
        ]
        chosen_id, template_debug = await llm_service.pick_transfer_template(
            request=prompt, templates=catalog
        )
        template = template_ids.get(chosen_id) if chosen_id else None
        if template is None:
            raise ValidationFailed(
                "Не удалось подобрать подходящий шаблон по запросу — "
                "уточните формулировку."
            )

        # ---- Call 2: pick participants ----
        # Only data the caller may see is offered to the model — otherwise a
        # hidden group's test data would be sent to the LLM and rendered back to
        # someone who never unlocked it.
        clients = await self.clients.list_all(visible_group_ids=visible_group_ids)
        accounts = await self.accounts.list_all(visible_group_ids=visible_group_ids)
        cards = await self.cards.list_all(visible_group_ids=visible_group_ids)

        client_ids = {f"C{i}": c for i, c in enumerate(clients, start=1)}
        account_ids = {f"A{i}": a for i, a in enumerate(accounts, start=1)}
        card_ids = {f"K{i}": k for i, k in enumerate(cards, start=1)}
        # reverse maps so we can render parent short-ids in the catalogs
        client_short = {c.id: sid for sid, c in client_ids.items()}
        account_short = {a.id: sid for sid, a in account_ids.items()}

        clients_catalog = [
            {
                "id": sid,
                "traits": _client_traits(c.attributes or {}),
                "description": _with_tags(c.description, c.tags),
            }
            for sid, c in client_ids.items()
        ]
        accounts_catalog = [
            {
                "id": sid,
                "client": client_short.get(a.client_id, ""),
                "currency": _currency(a),
                "description": _with_tags(a.description, a.tags),
            }
            for sid, a in account_ids.items()
        ]
        cards_catalog = [
            {
                "id": sid,
                "account": account_short.get(k.account_id, ""),
                "description": _with_tags(k.description, k.tags),
            }
            for sid, k in card_ids.items()
        ]

        need_account_owner = template_has_account_owner(template)
        picks, participants_debug = await llm_service.pick_transfer_participants(
            request=prompt,
            clients=clients_catalog,
            accounts=accounts_catalog,
            cards=cards_catalog,
            need_account_owner=need_account_owner,
        )

        resolved = self._resolve_picks(
            picks,
            client_ids=client_ids,
            account_ids=account_ids,
            card_ids=card_ids,
            allow_account_owner=need_account_owner,
        )
        if not resolved:
            raise ValidationFailed(
                "Не удалось подобрать участников по запросу — уточните формулировку "
                "или проверьте описания тестовых данных."
            )

        fill_kwargs = self._fill_kwargs(resolved)
        rendered, unresolved, changed = await PlaceholderFiller(self.session).fill_template(
            template, **fill_kwargs
        )
        rendered_html = render_filled_html(template.format, rendered, changed)

        return {
            "template": template,
            "roles": self._role_display(resolved),
            "rendered": rendered,
            "rendered_html": rendered_html,
            "unresolved": unresolved,
            "fill_qs": urlencode({k: str(v) for k, v in fill_kwargs.items() if v}),
            "llm_debug": self._merge_debug(
                template=template_debug, participants=participants_debug
            ),
        }

    @staticmethod
    def _merge_debug(
        *, template: dict[str, str], participants: dict[str, str]
    ) -> dict[str, str]:
        """Combine the two LLM calls (template pick, participants pick) into the
        flat 3-key shape the debug panel renders, with a labelled section per
        call — mirroring :meth:`app.llm.service.LLMService._merge_debug`."""

        keys = ("system_prompt", "user_prompt", "response_text")
        return {
            key: (
                f"### Подбор шаблона\n{template.get(key, '')}\n\n"
                f"### Подбор участников\n{participants.get(key, '')}"
            )
            for key in keys
        }

    def _resolve_picks(
        self,
        picks: dict[str, dict[str, str | None]],
        *,
        client_ids: dict[str, Client],
        account_ids: dict[str, Account],
        card_ids: dict[str, Card],
        allow_account_owner: bool,
    ) -> dict[str, dict[str, Any]]:
        """Expand short-ids to entities, dropping anything inconsistent.

        A mismatched account/card (not owned by the chosen client) is silently
        dropped rather than raising — the filler then falls back to the client's
        first account/card, which keeps a near-miss from failing the whole flow.
        """

        accounts_by_uuid = {a.id: a for a in account_ids.values()}
        resolved: dict[str, dict[str, Any]] = {}
        for role, pick in picks.items():
            if role == "accountOwner" and not allow_account_owner:
                continue
            client = client_ids.get(pick.get("client") or "")
            if client is None:
                continue
            entry: dict[str, Any] = {"client": client, "account": None, "card": None}

            account = account_ids.get(pick.get("account") or "")
            if account is not None and account.client_id == client.id:
                entry["account"] = account

            card = card_ids.get(pick.get("card") or "")
            if card is not None and self._card_fits(card, entry["account"], client, accounts_by_uuid):
                entry["card"] = card
            resolved[role] = entry
        return resolved

    @staticmethod
    def _card_fits(
        card: Card,
        account: Account | None,
        client: Client,
        accounts_by_uuid: dict[uuid.UUID, Account],
    ) -> bool:
        """A card is acceptable when it belongs to the chosen account (if any),
        or otherwise to any account owned by the chosen client."""

        if account is not None:
            return card.account_id == account.id
        parent = accounts_by_uuid.get(card.account_id)
        return parent is not None and parent.client_id == client.id

    @staticmethod
    def _fill_kwargs(resolved: dict[str, dict[str, Any]]) -> dict[str, uuid.UUID | None]:
        kwargs: dict[str, uuid.UUID | None] = {
            key: None for keys in ROLE_FILL_KEYS.values() for key in keys
        }
        for role, entry in resolved.items():
            client_key, account_key, card_key = ROLE_FILL_KEYS[role]
            kwargs[client_key] = entry["client"].id
            if entry["account"] is not None:
                kwargs[account_key] = entry["account"].id
            if entry["card"] is not None:
                kwargs[card_key] = entry["card"].id
        return kwargs

    def _role_display(self, resolved: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        from app.routes.entities_htmx import entity_label

        rows: list[dict[str, Any]] = []
        for role in ("sender", "receiver", "accountOwner"):
            entry = resolved.get(role)
            if entry is None:
                continue
            client = entry["client"]
            account = entry["account"]
            card = entry["card"]
            rows.append(
                {
                    "role": role,
                    "title": ROLE_TITLES[role],
                    "client_label": entity_label("client", client),
                    "traits": _client_traits(client.attributes or {}),
                    "account_label": entity_label("account", account) if account else None,
                    "account_currency": _currency(account) if account else None,
                    "card_label": entity_label("card", card) if card else None,
                }
            )
        return rows
