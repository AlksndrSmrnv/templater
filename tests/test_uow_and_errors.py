from __future__ import annotations

import json
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import main
from app.db.models import AttributeDefinition, Client, ReferenceValue
from app.routes.uow import commit_and_refresh, commit_or_409
from app.schemas.attribute import AttributeDefinitionCreate
from app.schemas.entity import ClientCreate
from app.schemas.reference import ReferenceValueCreate
from app.services import attribute_schema, entities, references
from app.utils.errors import IntegrityViolation


class FailingCommitSession:
    def __init__(self) -> None:
        self.rolled_back = False

    async def commit(self) -> None:
        raise IntegrityError("commit", None, Exception("duplicate"))

    async def rollback(self) -> None:
        self.rolled_back = True


class RecordingSession:
    def __init__(self) -> None:
        self.commits = 0
        self.refreshed: object | None = None
        self.rolled_back = False

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rolled_back = True

    async def refresh(self, item: object) -> None:
        self.refreshed = item


@pytest.mark.asyncio
async def test_commit_or_409_rolls_back_integrity_error() -> None:
    session = FailingCommitSession()

    with pytest.raises(IntegrityViolation):
        await commit_or_409(cast(AsyncSession, session))

    assert session.rolled_back is True


@pytest.mark.asyncio
async def test_commit_and_refresh_commits_then_refreshes_item() -> None:
    session = RecordingSession()
    item = object()

    result = await commit_and_refresh(cast(AsyncSession, session), item)

    assert result is item
    assert session.commits == 1
    assert session.refreshed is item
    assert session.rolled_back is False


class FakeClientRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, client: Client) -> Client:
        return client


class FakeAttributeSchemaService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def validate_attributes(self, entity_type: str, values: dict[str, Any]) -> dict[str, Any]:
        return dict(values)


@pytest.mark.asyncio
async def test_client_service_create_does_not_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    session = RecordingSession()
    monkeypatch.setattr(entities, "ClientRepository", FakeClientRepository)
    monkeypatch.setattr(entities, "AttributeSchemaService", FakeAttributeSchemaService)

    client = await entities.ClientService(cast(AsyncSession, session)).create(
        ClientCreate(attributes={"fullName": "Alice"})
    )

    assert client.attributes == {"fullName": "Alice"}
    assert session.commits == 0
    assert session.refreshed is None


class FailingReferenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_code(self, entity_type: str, code: str) -> None:
        return None

    async def add(self, value: ReferenceValue) -> ReferenceValue:
        raise IntegrityError("insert reference", None, Exception("duplicate"))


@pytest.mark.asyncio
async def test_reference_create_wraps_unique_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = RecordingSession()
    monkeypatch.setattr(references, "ReferenceValueRepository", FailingReferenceRepository)
    monkeypatch.setattr(references, "AttributeSchemaService", FakeAttributeSchemaService)

    with pytest.raises(IntegrityViolation):
        await references.ReferenceService(cast(AsyncSession, session)).create(
            ReferenceValueCreate(entity_type="currency", code="USD", name="US Dollar")
        )

    assert session.rolled_back is True


class FailingAttributeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_name(self, entity_type: str, name: str) -> None:
        return None

    async def add(
        self,
        attr: AttributeDefinition,
    ) -> AttributeDefinition:
        raise IntegrityError("insert attr", None, Exception("duplicate"))


@pytest.mark.asyncio
async def test_attribute_create_wraps_unique_race(monkeypatch: pytest.MonkeyPatch) -> None:
    session = RecordingSession()
    monkeypatch.setattr(attribute_schema, "AttributeDefinitionRepository", FailingAttributeRepository)

    with pytest.raises(IntegrityViolation):
        await attribute_schema.AttributeSchemaService(cast(AsyncSession, session)).create(
            AttributeDefinitionCreate(
                entity_type="client",
                name="fullName",
                label="Full name",
                data_type="string",
            )
        )

    assert session.rolled_back is True


def test_unexpected_exception_handler_returns_neutral_500() -> None:
    app = main.create_app()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("sensitive details")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")

    assert response.status_code == 500
    assert response.json() == {
        "error": "internal_server_error",
        "message": "Внутренняя ошибка сервера",
    }


class _NoopSession:
    """Minimal AsyncSession stand-in for htmx_delete: only commit/rollback used."""

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _client_with_fake_session() -> TestClient:
    from app.routes.deps import db_session

    app = main.create_app()

    async def _fake_session() -> Any:
        yield cast(AsyncSession, _NoopSession())

    app.dependency_overrides[db_session] = _fake_session
    return TestClient(app, raise_server_exceptions=False)


def test_htmx_delete_with_relations_shows_error_toast(monkeypatch: pytest.MonkeyPatch) -> None:
    message = "К клиенту привязано счетов: 2. Удалите их сначала."

    async def failing_delete(self: Any, client_id: Any) -> None:
        raise IntegrityViolation(message, details={"dependent_accounts": 2})

    monkeypatch.setattr(entities.ClientService, "delete", failing_delete)

    with _client_with_fake_session() as client:
        response = client.delete(
            "/templater/entities-htmx/client/00000000-0000-0000-0000-000000000001"
        )

    # Status 200 (not 409) so htmx processes the HX-Trigger toast.
    assert response.status_code == 200
    trigger = json.loads(response.headers["HX-Trigger"])
    assert trigger["showToast"] == {"message": message, "type": "error"}
    # The success-path events must NOT fire on error.
    assert "refresh-entities" not in trigger
    assert "close-drawer" not in trigger


def test_htmx_delete_success_shows_done_toast(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok_delete(self: Any, client_id: Any) -> None:
        return None

    monkeypatch.setattr(entities.ClientService, "delete", ok_delete)

    with _client_with_fake_session() as client:
        response = client.delete(
            "/templater/entities-htmx/client/00000000-0000-0000-0000-000000000001"
        )

    assert response.status_code == 204
    trigger = json.loads(response.headers["HX-Trigger"])
    assert trigger["showToast"] == {"message": "Удалено", "type": "success"}
    assert trigger["refresh-entities"] is True
    assert trigger["close-drawer"] is True


def test_lifespan_shutdown_disposes_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    async def fake_shutdown_engine() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(main, "shutdown_engine", fake_shutdown_engine)

    with TestClient(main.create_app()):
        pass

    assert called is True
