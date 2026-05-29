from __future__ import annotations

import json

from starlette.responses import Response

from app.routes.htmx_utils import toast_header


def test_toast_header_value_is_latin1_safe() -> None:
    """HX-Trigger header values are encoded as latin-1 by Starlette, so the JSON
    must not contain raw non-ASCII characters (Cyrillic) — otherwise the response
    raises UnicodeEncodeError (HTTP 500) and htmx performs no swap/toast."""
    value = toast_header("Удалено", refresh_entities=True, close_drawer=True)

    # Must encode as a latin-1 HTTP header value without raising.
    value.encode("latin-1")

    payload = json.loads(value)
    assert payload["showToast"] == {"message": "Удалено", "type": "success"}
    # snake_case event kwargs are exposed as dash-cased htmx events.
    assert payload["refresh-entities"] is True
    assert payload["close-drawer"] is True


def test_toast_header_usable_as_response_header() -> None:
    """A real Starlette Response with a Cyrillic toast must build successfully."""
    response = Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Импортирована коллекция «Заказы»")},
    )
    raw = dict(response.raw_headers)
    assert b"hx-trigger" in raw
