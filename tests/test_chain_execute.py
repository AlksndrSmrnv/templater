"""The stub «send» seam (POST /send-htmx/execute) — NO real network request.

It echoes the step's editable example response back so the chain UI can
demonstrate the flow until the real sending tool lands over an API, and records
every send to the history (``message_sends``).
"""

from __future__ import annotations

import json

import pytest

from app.db.models import MessageSend
from app.routes import chains


class _FakeRequest:
    def __init__(self, payload: object, *, raise_error: bool = False) -> None:
        self._payload = payload
        self._raise = raise_error

    async def json(self) -> object:
        if self._raise:
            raise ValueError("not json")
        return self._payload


class _FakeSession:
    """Minimal AsyncSession stand-in: captures added rows, no-op flush/commit.

    ``get`` returns from a preloaded store so ``_record_send`` can verify a
    source id still exists (missing → dropped to NULL).
    """

    def __init__(self, store: dict | None = None) -> None:
        self.added: list = []
        self._store = store or {}
        self.committed = False

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None

    async def get(self, model: type, ident: object) -> object | None:
        return self._store.get((model, ident))

    @property
    def sends(self) -> list[MessageSend]:
        return [o for o in self.added if isinstance(o, MessageSend)]


@pytest.fixture(autouse=True)
def _no_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(latency_ms: int) -> None:
        return None

    monkeypatch.setattr(chains, "_simulate_latency", _noop)


@pytest.mark.asyncio
async def test_execute_echoes_mock_response() -> None:
    mock = '{"status": "SUCCESS", "transferId": "TRF-42"}'
    req = _FakeRequest(
        {"method": "POST", "url": "https://x", "headers": [], "body": "{}",
         "format": "json", "mock_response": mock}
    )
    resp = await chains.htmx_execute(req, _FakeSession())  # type: ignore[arg-type]
    assert resp.status_code == 200
    data = json.loads(bytes(resp.body))
    assert data["status"] == 200
    assert data["status_text"] == "OK"
    assert data["body"] == mock  # echoed verbatim — no real send
    assert "latency_ms" in data


@pytest.mark.asyncio
async def test_execute_records_successful_send() -> None:
    mock = '{"statusCode": 0, "transferId": "TRF-1"}'
    session = _FakeSession()
    req = _FakeRequest(
        {"method": "POST", "url": "https://x", "headers": [{"k": "v"}],
         "body": "{\"a\": 1}", "format": "json", "mock_response": mock,
         "source_kind": "filled", "name": "Платёж"}
    )
    await chains.htmx_execute(req, session)  # type: ignore[arg-type]
    assert len(session.sends) == 1
    rec = session.sends[0]
    assert rec.source_kind == "filled"
    assert rec.ok is True
    assert rec.status_code == 0
    assert rec.http_status == 200
    assert rec.url == "https://x"
    assert rec.request_body == '{"a": 1}'
    assert rec.response_body == mock
    assert rec.name_snapshot == "Платёж"
    assert session.committed is True


@pytest.mark.asyncio
async def test_execute_records_failure_on_nonzero_status_code() -> None:
    mock = '{"statusCode": 5, "error": "denied"}'
    session = _FakeSession()
    req = _FakeRequest(
        {"method": "POST", "url": "https://x", "headers": [], "body": "{}",
         "format": "json", "mock_response": mock, "source_kind": "chain_step"}
    )
    await chains.htmx_execute(req, session)  # type: ignore[arg-type]
    rec = session.sends[0]
    assert rec.ok is False
    assert rec.status_code == 5


@pytest.mark.asyncio
async def test_execute_drops_stale_source_id_but_keeps_source_kind() -> None:
    # A chain_step_id whose row no longer exists must not block the send — it is
    # stored as NULL — but the send is still a chain-step send: the client's
    # valid source_kind is preserved, not re-labelled «filled» by the id fallback.
    import uuid

    stale = uuid.uuid4()
    session = _FakeSession(store={})  # nothing in the store → get() returns None
    req = _FakeRequest(
        {"method": "GET", "url": "https://x", "headers": [], "body": "",
         "format": "json", "mock_response": "{}",
         "source_kind": "chain_step", "chain_step_id": str(stale)}
    )
    await chains.htmx_execute(req, session)  # type: ignore[arg-type]
    rec = session.sends[0]
    assert rec.chain_step_id is None
    assert rec.source_kind == "chain_step"


@pytest.mark.asyncio
async def test_execute_rejects_invalid_json() -> None:
    resp = await chains.htmx_execute(_FakeRequest(None, raise_error=True), _FakeSession())  # type: ignore[arg-type]
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_execute_rejects_non_object_body() -> None:
    resp = await chains.htmx_execute(_FakeRequest([1, 2, 3]), _FakeSession())  # type: ignore[arg-type]
    assert resp.status_code == 422
    resp2 = await chains.htmx_execute(_FakeRequest("scalar"), _FakeSession())  # type: ignore[arg-type]
    assert resp2.status_code == 422


def test_execute_route_registered() -> None:
    paths = {getattr(r, "path", "") for r in chains.router.routes}
    assert "/send-htmx/execute" in paths


def test_default_mock_response_is_utc_z_json() -> None:
    import json

    from app.services.request_chain import default_mock_response

    data = json.loads(default_mock_response())
    assert data["status"] == "SUCCESS"
    assert data["processedAt"].endswith("Z")
    assert "transferId" in data
