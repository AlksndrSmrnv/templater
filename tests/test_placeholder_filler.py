from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from xml.etree import ElementTree as ET

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.placeholders import PlaceholderFiller


def _session() -> AsyncSession:
    return cast(AsyncSession, None)


def test_fill_content_replaces_tokens_in_json() -> None:
    filler = PlaceholderFiller(session=_session())  # session unused for fill_content
    ctx = {
        "sender": {"fullName": "Иванов", "account": {"number": "40817-100"}},
        "receiver": {"fullName": "Петров", "card": {"number": "4276-...-0001"}},
    }
    content = (
        '{"from": "{{sender.fullName}}", "fromAccount": "{{sender.account.number}}",'
        ' "to": "{{receiver.fullName}}", "toCard": "{{receiver.card.number}}",'
        ' "note": "{{sender.unknown.path}}"}'
    )
    rendered, unresolved, changed = filler.fill_content(content, "json", ctx)
    # Result must still parse as JSON
    parsed = json.loads(rendered)
    assert parsed["from"] == "Иванов"
    assert parsed["fromAccount"] == "40817-100"
    assert parsed["to"] == "Петров"
    assert parsed["toCard"] == "4276-...-0001"
    assert parsed["note"] == "{{sender.unknown.path}}"  # not resolved — kept verbatim
    assert unresolved == ["sender.unknown.path"]
    assert set(changed) == {"/from", "/fromAccount", "/to", "/toCard"}
    assert "/note" not in changed


def test_fill_content_replaces_account_owner_client_tokens_in_json() -> None:
    filler = PlaceholderFiller(session=_session())
    ctx = {"accountOwner": {"firstName": "Иван"}}
    content = '{"ownerFirstName": "{{accountOwner.firstName}}"}'

    rendered, unresolved, changed = filler.fill_content(content, "json", ctx)

    parsed = json.loads(rendered)
    assert parsed["ownerFirstName"] == "Иван"
    assert unresolved == []
    assert changed == ["/ownerFirstName"]


def test_fill_content_escapes_special_chars_in_json() -> None:
    """Values with quotes / backslashes must not break the JSON envelope."""

    filler = PlaceholderFiller(session=_session())
    ctx = {"sender": {"fullName": 'John "The Boss" \\Doe'}}
    content = '{"name": "{{sender.fullName}}"}'
    rendered, unresolved, _ = filler.fill_content(content, "json", ctx)
    parsed = json.loads(rendered)
    assert parsed["name"] == 'John "The Boss" \\Doe'
    assert unresolved == []


def test_fill_content_escapes_special_chars_in_xml() -> None:
    """Values with < > & must be XML-escaped, not inserted raw."""

    filler = PlaceholderFiller(session=_session())
    ctx = {"sender": {"fullName": "John & <Mary>"}}
    content = "<msg><name>{{sender.fullName}}</name></msg>"
    rendered, _, _ = filler.fill_content(content, "xml", ctx)
    root = ET.fromstring(rendered)
    name = root.find("name")
    assert name is not None
    assert name.text == "John & <Mary>"


def test_fill_content_preserves_unresolved_tokens() -> None:
    filler = PlaceholderFiller(session=_session())
    rendered, unresolved, _ = filler.fill_content('{"a": "{{nope.x}}"}', "json", {})
    parsed = json.loads(rendered)
    assert parsed["a"] == "{{nope.x}}"
    assert unresolved == ["nope.x"]


def test_fill_content_preserves_dynamic_tokens_without_unresolved() -> None:
    filler = PlaceholderFiller(session=_session())
    rendered, unresolved, changed = filler.fill_content('{"rqUID": "{{rqUID}}"}', "json", {})
    parsed = json.loads(rendered)
    assert parsed["rqUID"] == "{{rqUID}}"
    assert unresolved == []
    assert changed == []


def test_fill_content_refuses_unparsable_json() -> None:
    """When the template doesn't parse as declared format, we must NOT silently
    fall back to raw-text substitution — that would re-introduce the escaping
    bug. The caller gets a ValidationFailed instead."""

    import pytest

    from app.utils.errors import ValidationFailed

    filler = PlaceholderFiller(session=_session())
    with pytest.raises(ValidationFailed):
        filler.fill_content("this is not json {{sender.x}}", "json", {"sender": {"x": "v"}})


def test_fill_content_refuses_unparsable_xml() -> None:
    from app.utils.errors import ValidationFailed

    filler = PlaceholderFiller(session=_session())
    with pytest.raises(ValidationFailed):
        filler.fill_content("<broken {{sender.x}}", "xml", {"sender": {"x": "v"}})


@pytest.mark.asyncio
async def test_build_context_includes_account_owner_role() -> None:
    filler = PlaceholderFiller(session=_session())
    owner_client_id = uuid.uuid4()

    async def fake_role_context(
        *,
        client_id: uuid.UUID | None,
        account_id: uuid.UUID | None,
        card_id: uuid.UUID | None,
    ) -> dict[str, str | None]:
        return {
            "client_id": str(client_id) if client_id else None,
            "account_id": str(account_id) if account_id else None,
            "card_id": str(card_id) if card_id else None,
        }

    filler._role_context = fake_role_context  # type: ignore[method-assign]

    ctx = await filler.build_context(
        sender_client_id=None,
        sender_account_id=None,
        sender_card_id=None,
        receiver_client_id=None,
        receiver_account_id=None,
        receiver_card_id=None,
        account_owner_client_id=owner_client_id,
        account_owner_account_id=None,
        account_owner_card_id=None,
    )

    assert ctx["accountOwner"]["client_id"] == str(owner_client_id)


class _FakeRepo:
    def __init__(
        self,
        by_id: dict[uuid.UUID, SimpleNamespace],
        list_factory: Callable[..., list[SimpleNamespace]] | None = None,
    ) -> None:
        self.by_id = by_id
        self.list_factory = list_factory or (lambda **_: [])
        self.list_calls: list[dict[str, object]] = []

    async def get(self, item_id: uuid.UUID) -> SimpleNamespace | None:
        return self.by_id.get(item_id)

    async def list_all(self, **kwargs: object) -> list[SimpleNamespace]:
        self.list_calls.append(kwargs)
        return self.list_factory(**kwargs)


@pytest.mark.asyncio
async def test_role_context_uses_card_account_when_account_id_is_omitted() -> None:
    client_id = uuid.uuid4()
    first_account_id = uuid.uuid4()
    card_account_id = uuid.uuid4()
    card_id = uuid.uuid4()
    client = SimpleNamespace(id=client_id, attributes={"fullName": "Owner"})
    first_account = SimpleNamespace(
        id=first_account_id,
        client_id=client_id,
        attributes={"number": "first-account"},
    )
    card_account = SimpleNamespace(
        id=card_account_id,
        client_id=client_id,
        attributes={"number": "card-account"},
    )
    card = SimpleNamespace(id=card_id, account_id=card_account_id, attributes={"number": "card-001"})

    filler = PlaceholderFiller(session=_session())
    filler.clients = _FakeRepo({client_id: client})  # type: ignore[assignment]
    filler.accounts = _FakeRepo(
        {first_account_id: first_account, card_account_id: card_account},
        lambda **_: [first_account],
    )  # type: ignore[assignment]
    fake_cards = _FakeRepo({card_id: card})
    filler.cards = fake_cards  # type: ignore[assignment]

    ctx = await filler._role_context(
        client_id=client_id,
        account_id=None,
        card_id=card_id,
    )

    assert ctx["account"]["number"] == "card-account"
    assert ctx["card"]["number"] == "card-001"
    assert fake_cards.list_calls == []
