"""Resolve ``{{sender.*}}`` / ``{{receiver.*}}`` placeholders against test data.

Given a template (with placeholders already applied to its content) and the
selected sender/receiver client+account+card, this builds the final message
body by substituting each placeholder token with concrete values.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account, Card, MessageTemplate
from app.repositories.entity import (
    AccountRepository,
    CardRepository,
    ClientRepository,
)
from app.utils import walker
from app.utils.errors import NotFoundError, ValidationFailed

TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


class PlaceholderFiller:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.clients = ClientRepository(session)
        self.accounts = AccountRepository(session)
        self.cards = CardRepository(session)

    async def build_context(
        self,
        *,
        sender_client_id: uuid.UUID | None,
        sender_account_id: uuid.UUID | None,
        sender_card_id: uuid.UUID | None,
        receiver_client_id: uuid.UUID | None,
        receiver_account_id: uuid.UUID | None,
        receiver_card_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        context: dict[str, Any] = {}
        context["sender"] = await self._role_context(
            client_id=sender_client_id, account_id=sender_account_id, card_id=sender_card_id
        )
        context["receiver"] = await self._role_context(
            client_id=receiver_client_id, account_id=receiver_account_id, card_id=receiver_card_id
        )
        return context

    async def _role_context(
        self,
        *,
        client_id: uuid.UUID | None,
        account_id: uuid.UUID | None,
        card_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if client_id is None:
            return out
        client = await self.clients.get(client_id)
        if client is None:
            raise NotFoundError("Клиент не найден")
        out.update(client.attributes or {})
        # Account: explicit OR first one for this client
        account: Account | None = None
        if account_id is not None:
            account = await self.accounts.get(account_id)
            if account is None or account.client_id != client.id:
                raise ValidationFailed("Счёт не принадлежит выбранному клиенту")
        else:
            accs = await self.accounts.list_all(client_id=client.id)
            account = accs[0] if accs else None
        if account is not None:
            out["account"] = dict(account.attributes or {})
            # Card
            card: Card | None = None
            if card_id is not None:
                card = await self.cards.get(card_id)
                if card is None or card.account_id != account.id:
                    raise ValidationFailed("Карта не принадлежит выбранному счёту")
            else:
                cards = await self.cards.list_all(account_id=account.id)
                card = cards[0] if cards else None
            if card is not None:
                out["card"] = dict(card.attributes or {})
        return out

    def fill_content(
        self, content: str, fmt: str, context: dict[str, Any]
    ) -> tuple[str, list[str], list[str]]:
        """Walk the parsed document and replace ``{{...}}`` tokens at the AST level.

        Working at the AST keeps user-supplied values from breaking the document
        envelope: JSON values are re-serialized via ``json.dumps`` (handling quotes,
        backslashes, unicode) and XML text/attributes are placed via ElementTree
        (handling ``< > & " '``). A regex sweep over the raw text would corrupt
        the document for any value containing those characters.
        """

        if fmt in ("json", "xml"):
            # For known formats we refuse to do raw-text substitution: silently
            # falling back would re-introduce the JSON/XML escaping bug. Surface
            # the parse error to the caller instead.
            try:
                leaves = walker.walk_json(content) if fmt == "json" else walker.walk_xml(content)
            except Exception as exc:
                raise ValidationFailed(
                    f"Шаблон не парсится как {fmt}, безопасная подстановка невозможна: {exc}"
                ) from exc
            return self._fill_via_walker(content, leaves, context, fmt=fmt)
        return self._textual_fill(content, context)

    def _fill_via_walker(
        self,
        content: str,
        leaves: list[walker.Leaf],
        context: dict[str, Any],
        *,
        fmt: str,
    ) -> tuple[str, list[str], list[str]]:
        replacements: dict[str, str] = {}
        unresolved: list[str] = []
        for leaf in leaves:
            if "{{" not in leaf.value:
                continue
            new_value, missing = self._expand_tokens(leaf.value, context)
            unresolved.extend(missing)
            if new_value != leaf.value:
                replacements[leaf.location] = new_value
        changed = list(replacements.keys())
        if not replacements:
            return content, unresolved, changed
        if fmt == "json":
            return walker.replace_json(content, replacements), unresolved, changed
        return walker.replace_xml(content, replacements), unresolved, changed

    def _textual_fill(self, content: str, context: dict[str, Any]) -> tuple[str, list[str], list[str]]:
        """Last-resort text substitution. Use only when structural parse fails."""

        rendered, unresolved = self._expand_tokens(content, context)
        return rendered, unresolved, []

    def _expand_tokens(self, text: str, context: dict[str, Any]) -> tuple[str, list[str]]:
        unresolved: list[str] = []

        def replace(match: re.Match[str]) -> str:
            path = match.group(1)
            value = self._resolve_path(context, path)
            if value is None:
                unresolved.append(path)
                return match.group(0)
            return str(value)

        return TOKEN_RE.sub(replace, text), unresolved

    @staticmethod
    def _resolve_path(context: dict[str, Any], dotted: str) -> Any:
        parts = dotted.split(".")
        node: Any = context
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return None
        return node

    async def fill_template(
        self,
        template: MessageTemplate,
        *,
        sender_client_id: uuid.UUID | None,
        sender_account_id: uuid.UUID | None,
        sender_card_id: uuid.UUID | None,
        receiver_client_id: uuid.UUID | None,
        receiver_account_id: uuid.UUID | None,
        receiver_card_id: uuid.UUID | None,
    ) -> tuple[str, list[str], list[str]]:
        ctx = await self.build_context(
            sender_client_id=sender_client_id,
            sender_account_id=sender_account_id,
            sender_card_id=sender_card_id,
            receiver_client_id=receiver_client_id,
            receiver_account_id=receiver_account_id,
            receiver_card_id=receiver_card_id,
        )
        return self.fill_content(template.content, template.format, ctx)
