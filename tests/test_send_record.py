"""The «send» history-recording endpoint (POST /send-htmx/record).

The browser itself performs (or mocks) the request — see
``app/static/js/rest_sender.js`` — and reports the outcome here; the endpoint
only persists the row to ``message_sends``. These tests call the handler
directly with fakes (no TestClient, no DB), the same style as the rest of the
endpoint tests.
"""

from __future__ import annotations

import json
import uuid

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

    def __init__(self, store: dict[tuple[type, object], object] | None = None) -> None:
        self.added: list[object] = []
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


def _payload(**overrides: object) -> dict[str, object]:
    """A complete browser-reported send: envelope + result block."""

    base: dict[str, object] = {
        "method": "POST",
        "url": "https://x",
        "headers": [{"key": "RqUID", "value": "1"}],
        "body": '{"a": 1}',
        "format": "json",
        "source_kind": "filled",
        "name": "Платёж",
        "result": {
            "ok": True,
            "http_status": 200,
            "status_code": 0,
            "headers": {"Content-Type": "application/json"},
            "body": '{"statusCode": 0, "transferId": "TRF-1"}',
            "latency_ms": 120,
            "error": "",
        },
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_record_persists_successful_send_verbatim() -> None:
    session = _FakeSession()
    resp = await chains.htmx_record(_FakeRequest(_payload()), session)  # type: ignore[arg-type]
    assert resp.status_code == 200
    assert json.loads(bytes(resp.body)) == {"recorded": True}
    assert len(session.sends) == 1
    rec = session.sends[0]
    assert rec.source_kind == "filled"
    assert rec.ok is True
    assert rec.http_status == 200
    assert rec.status_code == 0
    assert rec.url == "https://x"
    assert rec.request_headers == [{"key": "RqUID", "value": "1"}]
    assert rec.request_body == '{"a": 1}'
    assert rec.response_headers == {"Content-Type": "application/json"}
    assert rec.response_body == '{"statusCode": 0, "transferId": "TRF-1"}'
    assert rec.latency_ms == 120
    assert rec.name_snapshot == "Платёж"
    assert session.committed is True


@pytest.mark.asyncio
async def test_record_persists_transport_failure() -> None:
    # A failed real send has no status/body — only the seam's error text.
    session = _FakeSession()
    payload = _payload(result={
        "ok": False,
        "http_status": None,
        "status_code": None,
        "headers": {},
        "body": "",
        "latency_ms": 45,
        "error": "Запрос не ушёл из браузера…",
    })
    await chains.htmx_record(_FakeRequest(payload), session)  # type: ignore[arg-type]
    rec = session.sends[0]
    assert rec.ok is False
    assert rec.http_status is None
    assert rec.status_code is None
    assert rec.response_body == ""
    assert rec.error_message == "Запрос не ушёл из браузера…"


@pytest.mark.asyncio
async def test_record_coerces_junk_result_fields() -> None:
    # Client-reported numbers are coerced defensively: booleans must not sneak
    # in as HTTP statuses, non-dict headers / non-str body become empty.
    session = _FakeSession()
    # NaN/Infinity: a browser can't produce them (JSON.stringify → null), but
    # json.loads accepts them — they must degrade to NULL, not raise.
    payload = _payload(result={
        "ok": 1,
        "http_status": True,
        "status_code": "5",
        "headers": ["not", "a", "dict"],
        "body": {"not": "a string"},
        "latency_ms": float("nan"),
        "error": None,
    })
    await chains.htmx_record(_FakeRequest(payload), session)  # type: ignore[arg-type]
    rec = session.sends[0]
    assert rec.ok is True
    assert rec.http_status is None
    assert rec.status_code is None
    assert rec.response_headers == {}
    assert rec.response_body == ""
    assert rec.error_message == ""
    assert rec.latency_ms is None


@pytest.mark.asyncio
async def test_record_coerces_infinite_latency_to_null() -> None:
    session = _FakeSession()
    payload = _payload()
    result = payload["result"]
    assert isinstance(result, dict)
    result["latency_ms"] = float("inf")
    await chains.htmx_record(_FakeRequest(payload), session)  # type: ignore[arg-type]
    assert session.sends[0].latency_ms is None


@pytest.mark.asyncio
async def test_record_drops_stale_source_id_but_keeps_source_kind() -> None:
    # A chain_step_id whose row no longer exists must not block the recording —
    # it is stored as NULL — but the send is still a chain-step send: the
    # client's valid source_kind is preserved, not re-labelled «filled».
    stale = uuid.uuid4()
    session = _FakeSession(store={})  # nothing in the store → get() returns None
    payload = _payload(source_kind="chain_step", chain_step_id=str(stale))
    await chains.htmx_record(_FakeRequest(payload), session)  # type: ignore[arg-type]
    rec = session.sends[0]
    assert rec.chain_step_id is None
    assert rec.source_kind == "chain_step"


@pytest.mark.asyncio
async def test_record_rejects_invalid_json() -> None:
    resp = await chains.htmx_record(_FakeRequest(None, raise_error=True), _FakeSession())  # type: ignore[arg-type]
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_record_rejects_non_object_body() -> None:
    resp = await chains.htmx_record(_FakeRequest([1, 2, 3]), _FakeSession())  # type: ignore[arg-type]
    assert resp.status_code == 422
    resp2 = await chains.htmx_record(_FakeRequest("scalar"), _FakeSession())  # type: ignore[arg-type]
    assert resp2.status_code == 422


@pytest.mark.asyncio
async def test_record_rejects_missing_result_block() -> None:
    payload = _payload()
    del payload["result"]
    resp = await chains.htmx_record(_FakeRequest(payload), _FakeSession())  # type: ignore[arg-type]
    assert resp.status_code == 422
    resp2 = await chains.htmx_record(
        _FakeRequest(_payload(result="not a dict")), _FakeSession()  # type: ignore[arg-type]
    )
    assert resp2.status_code == 422


def test_record_route_registered_and_execute_gone() -> None:
    paths = {getattr(r, "path", "") for r in chains.router.routes}
    assert "/send-htmx/record" in paths
    # The old server-side send seam is gone — the browser sends, never the server.
    assert "/send-htmx/execute" not in paths


def test_default_mock_response_is_utc_z_json() -> None:
    from app.services.request_chain import default_mock_response

    data = json.loads(default_mock_response())
    assert data["status"] == "SUCCESS"
    assert data["processedAt"].endswith("Z")
    assert "transferId" in data
    # statusCode 0 so the green «statusCode = 0» indicator shows by default.
    assert data["statusCode"] == 0
