"""Unit tests for the access-groups feature (password vaults for test data).

No live DB (see the project's test conventions): token/password helpers and the
SQL visibility predicates are pure logic; the service is exercised with a fake
repository. End-to-end filtering is verified separately against docker Postgres.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.db.models import Account, Card, Client, FilledTemplate
from app.repositories.entity import group_visibility_condition
from app.routes.deps import get_templates
from app.schemas.access_group import AccessGroupCreate, AccessGroupUpdate
from app.schemas.template import TemplateFillRequest
from app.services.access_groups import AccessGroupService
from app.services.filled_templates import FilledTemplateService
from app.utils import access_groups as ag
from app.utils.errors import IntegrityViolation, ValidationFailed
from app.utils.password import hash_password, verify_password


def render_template(name: str, context: dict[str, object]) -> str:
    return get_templates().env.get_template(name).render(context)


# --------------------------- password hashing ---------------------------


def test_password_round_trip_and_salting() -> None:
    h1 = hash_password("hunter2")
    h2 = hash_password("hunter2")
    # Random per-call salt → different stored strings for the same password.
    assert h1 != h2
    assert h1.startswith("pbkdf2_sha256$")
    assert verify_password("hunter2", h1)
    assert verify_password("hunter2", h2)
    assert not verify_password("wrong", h1)


def test_password_rejects_empty_and_malformed() -> None:
    with pytest.raises(ValueError):
        hash_password("")
    assert not verify_password("x", "")
    assert not verify_password("", hash_password("x"))
    assert not verify_password("x", "not-a-valid-hash")
    assert not verify_password("x", "bogus$1$1$1")


# --------------------------- cookie token ---------------------------


def test_token_round_trip_preserves_unlocked_set() -> None:
    ids = {uuid.uuid4(), uuid.uuid4()}
    token = ag.issue_groups_token(ids)
    assert ag._parse(token) == ids


def test_token_is_order_independent() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    assert ag.issue_groups_token([a, b]) == ag.issue_groups_token([b, a])


def test_token_rejects_tampering() -> None:
    ids = {uuid.uuid4()}
    token = ag.issue_groups_token(ids)
    payload, _, sig = token.rpartition(".")
    # Add an id the server never signed → signature no longer matches.
    forged = f"{payload},{uuid.uuid4().hex}.{sig}"
    assert ag._parse(forged) == set()
    # Flip the signature.
    assert ag._parse(f"{payload}.{'0' * len(sig)}") == set()


def test_token_expires() -> None:
    ids = {uuid.uuid4()}
    token = ag.issue_groups_token(ids, now=1000.0)
    # Just before expiry → still valid; after → empty.
    assert ag._parse(token, now=1000.0 + ag.TOKEN_TTL_SECONDS - 1) == ids
    assert ag._parse(token, now=1000.0 + ag.TOKEN_TTL_SECONDS + 1) == set()


def test_unlocked_group_ids_reads_cookie() -> None:
    ids = {uuid.uuid4()}
    token = ag.issue_groups_token(ids)
    request = SimpleNamespace(cookies={ag.COOKIE_NAME: token})
    assert ag.unlocked_group_ids(request) == ids
    assert ag.unlocked_group_ids(SimpleNamespace(cookies={})) == set()


# --------------------------- visibility SQL ---------------------------


def _compile(stmt: Any) -> str:
    return " ".join(str(stmt.compile(dialect=postgresql.dialect())).split())


def test_visibility_predicate_compiles_for_every_model() -> None:
    ids = {uuid.uuid4()}
    # Client / FilledTemplate filter on their own column…
    for model in (Client, FilledTemplate):
        sql = _compile(select(model.id).where(group_visibility_condition(model, ids)))
        assert "group_id IS NULL" in sql
        assert "group_id IN" in sql
    # …accounts/cards inherit via correlated EXISTS on the parent client.
    assert "EXISTS" in _compile(select(Account.id).where(group_visibility_condition(Account, ids)))
    assert _compile(select(Card.id).where(group_visibility_condition(Card, ids))).count("EXISTS") == 2


def test_visibility_none_means_unrestricted() -> None:
    assert group_visibility_condition(Client, None) is None


def test_visibility_empty_set_is_public_only() -> None:
    sql = _compile(select(Client.id).where(group_visibility_condition(Client, set())))
    assert "group_id IS NULL" in sql
    assert "IN (" not in sql  # no membership branch when nothing is unlocked


# --------------------------- service ---------------------------


class _FakeGroupRepo:
    """Minimal in-memory AccessGroupRepository; models flush() like the real one
    (autoflush is off, see the project's session-autoflush note)."""

    def __init__(self) -> None:
        self.items: list[Any] = []
        self.client_counts: dict[uuid.UUID, int] = {}
        self.filled_counts: dict[uuid.UUID, int] = {}

    async def list_all(self) -> list[Any]:
        return list(self.items)

    async def get(self, group_id: uuid.UUID) -> Any | None:
        return next((g for g in self.items if g.id == group_id), None)

    async def get_by_name(self, name: str) -> Any | None:
        return next((g for g in self.items if g.name == name), None)

    async def add(self, group: Any) -> Any:
        if group.id is None:
            group.id = uuid.uuid4()
        self.items.append(group)
        return group

    async def delete(self, group: Any) -> None:
        self.items.remove(group)

    async def count_clients(self, group_id: uuid.UUID) -> int:
        return self.client_counts.get(group_id, 0)

    async def count_filled(self, group_id: uuid.UUID) -> int:
        return self.filled_counts.get(group_id, 0)


def _service_with_fake() -> tuple[AccessGroupService, _FakeGroupRepo]:
    svc = AccessGroupService(session=SimpleNamespace(flush=_noop))  # type: ignore[arg-type]
    repo = _FakeGroupRepo()
    svc.repo = repo  # type: ignore[assignment]
    return svc, repo


async def _noop() -> None:
    return None


@pytest.mark.asyncio
async def test_create_hashes_password_and_unlock_matches() -> None:
    svc, repo = _service_with_fake()
    group = await svc.create(AccessGroupCreate(name="QA", color="#112233", password="secret"))
    # Password is never stored in clear; only a verifiable hash.
    assert group.password_hash != "secret"
    assert verify_password("secret", group.password_hash)

    assert (await svc.unlock("secret")).id == group.id
    assert await svc.unlock("nope") is None
    assert await svc.unlock("") is None


@pytest.mark.asyncio
async def test_create_rejects_duplicate_name() -> None:
    svc, _ = _service_with_fake()
    await svc.create(AccessGroupCreate(name="QA", color="#112233", password="a"))
    with pytest.raises(ValidationFailed):
        await svc.create(AccessGroupCreate(name="QA", color="#445566", password="b"))


@pytest.mark.asyncio
async def test_update_changes_password_only_when_supplied() -> None:
    svc, _ = _service_with_fake()
    group = await svc.create(AccessGroupCreate(name="QA", color="#112233", password="old"))
    original = group.password_hash

    # Blank password → unchanged.
    await svc.update(group.id, AccessGroupUpdate(name="QA-2", password=None))
    assert group.password_hash == original
    assert group.name == "QA-2"

    # Real password → rehashed.
    await svc.update(group.id, AccessGroupUpdate(password="new"))
    assert group.password_hash != original
    assert verify_password("new", group.password_hash)


@pytest.mark.asyncio
async def test_delete_refused_while_data_references_group() -> None:
    svc, repo = _service_with_fake()
    group = await svc.create(AccessGroupCreate(name="QA", color="#112233", password="a"))
    repo.client_counts[group.id] = 3
    with pytest.raises(IntegrityViolation):
        await svc.delete(group.id)
    # Once nothing references it, deletion succeeds.
    repo.client_counts[group.id] = 0
    await svc.delete(group.id)
    assert await repo.get(group.id) is None


# --------------------------- templates ---------------------------


def test_groups_navbar_hidden_when_no_groups() -> None:
    html = render_template("partials/groups_navbar.html", {"unlocked": [], "any_groups": False})
    assert "Разблокировать" not in html


def test_groups_navbar_shows_unlocked_badges_and_unlock_button() -> None:
    g = SimpleNamespace(id=uuid.uuid4(), name="QA", color="#7E57C2")
    html = render_template("partials/groups_navbar.html", {"unlocked": [g], "any_groups": True})
    assert "QA" in html
    assert f"/templater/groups-htmx/lock/{g.id}" in html
    assert "/templater/groups-htmx/unlock" in html


def test_groups_table_shows_password_field_only_in_edit_mode() -> None:
    g = SimpleNamespace(id=uuid.uuid4(), name="QA", color="#7E57C2")
    locked = render_template("partials/groups_table.html", {"groups": [g], "edit_mode": False})
    assert 'type="password"' not in locked
    unlocked = render_template("partials/groups_table.html", {"groups": [g], "edit_mode": True})
    assert 'type="password"' in unlocked
    assert "Добавить группу" in unlocked


# --------- filled-template group derivation across all roles ---------


class _FakeClientSession:
    """Session stand-in whose ``get`` resolves clients by id — enough for
    FilledTemplateService._fill_group (it builds a ClientRepository over it)."""

    def __init__(self, clients: list[Any]) -> None:
        self._by_id = {c.id: c for c in clients}

    async def get(self, model: Any, ident: Any) -> Any:
        return self._by_id.get(ident)


def _grouped_client(group_id: Any) -> SimpleNamespace:
    group = (
        SimpleNamespace(name=f"G-{str(group_id)[:4]}", color="#123456")
        if group_id is not None
        else None
    )
    return SimpleNamespace(id=uuid.uuid4(), group_id=group_id, group=group)


def _fill_service(clients: list[Any]) -> FilledTemplateService:
    svc = FilledTemplateService.__new__(FilledTemplateService)
    svc.session = _FakeClientSession(clients)  # type: ignore[attr-defined]
    return svc


@pytest.mark.asyncio
async def test_fill_group_public_when_no_role_has_a_group() -> None:
    sender = _grouped_client(None)
    svc = _fill_service([sender])
    gid, name, color = await svc._fill_group(TemplateFillRequest(sender_client_id=sender.id))
    assert (gid, name, color) == (None, "", "")


@pytest.mark.asyncio
async def test_fill_group_derives_from_receiver_when_sender_is_public() -> None:
    # The old bug: a private receiver with a public/absent sender was saved as
    # public. The group must come from *any* role, not just the sender.
    group_id = uuid.uuid4()
    receiver = _grouped_client(group_id)
    svc = _fill_service([receiver])
    gid, name, color = await svc._fill_group(
        TemplateFillRequest(receiver_client_id=receiver.id)
    )
    assert gid == group_id
    assert name and color  # snapshotted from the receiver's group


@pytest.mark.asyncio
async def test_fill_group_rejects_cross_group_mix() -> None:
    sender = _grouped_client(uuid.uuid4())
    receiver = _grouped_client(uuid.uuid4())  # a different group
    svc = _fill_service([sender, receiver])
    with pytest.raises(ValidationFailed):
        await svc._fill_group(
            TemplateFillRequest(sender_client_id=sender.id, receiver_client_id=receiver.id)
        )


@pytest.mark.asyncio
async def test_fill_group_allows_same_group_across_roles() -> None:
    group_id = uuid.uuid4()
    sender = _grouped_client(group_id)
    receiver = _grouped_client(group_id)  # same group → fine
    svc = _fill_service([sender, receiver])
    gid, _name, _color = await svc._fill_group(
        TemplateFillRequest(sender_client_id=sender.id, receiver_client_id=receiver.id)
    )
    assert gid == group_id
