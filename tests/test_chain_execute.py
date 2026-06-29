"""The stub «send» seam (POST /send-htmx/execute) — NO real network request.

It echoes the step's editable example response back so the chain UI can
demonstrate the flow until the real sending tool lands over an API.
"""

from __future__ import annotations

import json

import pytest

from app.routes import chains


class _FakeRequest:
    def __init__(self, payload: object, *, raise_error: bool = False) -> None:
        self._payload = payload
        self._raise = raise_error

    async def json(self) -> object:
        if self._raise:
            raise ValueError("not json")
        return self._payload


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
    resp = await chains.htmx_execute(req)  # type: ignore[arg-type]
    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["status"] == 200
    assert data["status_text"] == "OK"
    assert data["body"] == mock  # echoed verbatim — no real send
    assert "latency_ms" in data


@pytest.mark.asyncio
async def test_execute_rejects_invalid_json() -> None:
    resp = await chains.htmx_execute(_FakeRequest(None, raise_error=True))  # type: ignore[arg-type]
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_execute_rejects_non_object_body() -> None:
    resp = await chains.htmx_execute(_FakeRequest([1, 2, 3]))  # type: ignore[arg-type]
    assert resp.status_code == 422
    resp2 = await chains.htmx_execute(_FakeRequest("scalar"))  # type: ignore[arg-type]
    assert resp2.status_code == 422


def test_execute_route_registered() -> None:
    paths = {r.path for r in chains.router.routes}
    assert "/send-htmx/execute" in paths


def test_default_mock_response_is_utc_z_json() -> None:
    import json

    from app.services.request_chain import default_mock_response

    data = json.loads(default_mock_response())
    assert data["status"] == "SUCCESS"
    assert data["processedAt"].endswith("Z")
    assert "transferId" in data
