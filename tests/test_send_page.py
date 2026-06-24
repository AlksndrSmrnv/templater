from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.main import create_app
from app.routes import send
from app.routes.deps import get_templates


def render_template(name: str, context: dict[str, object]) -> str:
    return get_templates().env.get_template(name).render(context)


def _filled(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "3db678b1-1111-2222-3333-444444444444",
        "name": "A2A Transfer",
        "method": "POST",
        "project_name": "Альфа",
        "project_color": "#112233",
    }
    base.update(overrides)
    return base


# ---------- rendering ----------


def test_send_page_renders_chain_builder() -> None:
    html = render_template(
        "send.html", {"active": "send", "filled_templates": [_filled()]}
    )

    # Alpine chain component is wired with both endpoints.
    assert 'x-data="sendChain(' in html
    assert "/templater/send-htmx/filled/" in html
    assert "/templater/send-htmx/execute" in html
    # Toolbar + key controls present.
    assert "Добавить шаг" in html
    assert "Запустить всё" in html
    # The reference-token example renders as a literal {{ $1.transferId }} via Jinja.
    assert "{{ $1.transferId }}" in html
    # Seeded filled-templates list is JSON-encoded and HTML-escaped inside x-data.
    assert "&#34;" in html


def test_send_page_x_data_seed_is_html_escaped_not_a_breakout() -> None:
    # A name with double quotes must be JSON-escaped then HTML-escaped so it stays
    # inside the double-quoted x-data attribute instead of terminating it.
    html = render_template(
        "send.html",
        {"active": "send", "filled_templates": [_filled(name='A2A "VIP"')]},
    )
    assert 'A2A "VIP"' not in html  # no raw double-quote breakout
    assert "&#34;" in html


def test_send_page_x_data_neutralises_malicious_name() -> None:
    html = render_template(
        "send.html",
        {"active": "send", "filled_templates": [_filled(name='"});alert(1)//')]},
    )
    assert '"});alert' not in html
    assert "&#34;" in html


def test_send_nav_link_is_enabled() -> None:
    html = render_template("base.html", {"active": "send"})
    assert 'href="/templater/send"' in html
    assert "в разработке" not in html


def test_home_send_card_is_enabled() -> None:
    html = render_template("home.html", {"active": "home", "llm_active": False})
    assert 'class="card disabled" href="/templater/send"' not in html
    assert 'href="/templater/send"' in html
    assert "Цепочки запросов" in html


# ---------- route registration ----------


def test_send_routes_registered_under_templater_prefix() -> None:
    paths = {route.path for route in create_app().routes}
    assert "/templater/send" in paths
    assert "/templater/send-htmx/filled/{filled_id}" in paths
    assert "/templater/send-htmx/execute" in paths


# ---------- handlers (fakes, no DB) ----------


class _FakeTemplates:
    def TemplateResponse(self, request: object, name: str, context: dict[str, object]) -> Any:
        return SimpleNamespace(name=name, context=context)


@pytest.mark.asyncio
async def test_page_send_seeds_lightweight_filled_list(monkeypatch: pytest.MonkeyPatch) -> None:
    fid = uuid.uuid4()
    item = SimpleNamespace(
        id=fid,
        name="A2A",
        http_method_snapshot="POST",
        project_name_snapshot="Альфа",
        project_color_snapshot="#112233",
    )

    class FakeService:
        def __init__(self, session: object) -> None:
            pass

        async def list_all(self, *, visible_group_ids: object = None) -> list[object]:
            return [item]

    monkeypatch.setattr(send, "FilledTemplateService", FakeService)

    resp = cast(
        Any,
        await send.page_send(
            request=cast(Any, SimpleNamespace()),
            templates=cast(Any, _FakeTemplates()),
            session=cast(Any, SimpleNamespace()),
            group_ids=set(),
        ),
    )

    assert resp.name == "send.html"
    assert resp.context["filled_templates"] == [
        {
            "id": str(fid),
            "name": "A2A",
            "method": "POST",
            "project_name": "Альфа",
            "project_color": "#112233",
        }
    ]


@pytest.mark.asyncio
async def test_htmx_filled_snapshot_returns_request_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fid = uuid.uuid4()
    item = SimpleNamespace(
        id=fid,
        name="A2A",
        http_method_snapshot="POST",
        url_snapshot="https://api.bank.test/transfer",
        headers_snapshot=[{"key": "RqUID", "value": "{{rqUID}}", "mode": "dynamic"}],
        format="json",
        filled_content='{"amount": 100}',
        project_name_snapshot="Альфа",
        project_color_snapshot="#112233",
    )

    class FakeService:
        def __init__(self, session: object) -> None:
            pass

        async def get(self, filled_id: uuid.UUID, *, visible_group_ids: object = None) -> object:
            assert filled_id == fid
            return item

    monkeypatch.setattr(send, "FilledTemplateService", FakeService)

    resp = await send.htmx_filled_snapshot(
        fid, session=cast(Any, SimpleNamespace()), group_ids=set()
    )
    data = json.loads(resp.body)
    assert data["method"] == "POST"
    assert data["url"] == "https://api.bank.test/transfer"
    assert data["headers"] == [{"key": "RqUID", "value": "{{rqUID}}", "mode": "dynamic"}]
    assert data["format"] == "json"
    assert data["body"] == '{"amount": 100}'


@pytest.mark.asyncio
async def test_htmx_execute_is_a_stub_that_echoes_mock_response() -> None:
    class FakeRequest:
        async def json(self) -> dict[str, object]:
            return {
                "method": "POST",
                "url": "https://api.bank.test/transfer",
                "mock_response": '{"transferId": "TRF-1"}',
            }

    resp = await send.htmx_execute(cast(Any, FakeRequest()))
    data = json.loads(resp.body)
    assert data["status"] == 200
    assert data["status_text"] == "OK"
    assert isinstance(data["latency_ms"], int)
    assert data["headers"]["X-Mock-Send"] == "true"
    # The "response" is exactly the editable mock body the client sent.
    assert data["body"] == '{"transferId": "TRF-1"}'
