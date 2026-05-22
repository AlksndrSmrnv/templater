from __future__ import annotations

from fastapi.routing import APIRoute
from starlette.routing import Match

from app.routes.templates_reg import router


def first_full_match_path(path: str, method: str = "GET") -> str:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }

    for route in router.routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            assert isinstance(route, APIRoute)
            return route.path

    raise AssertionError(f"No full route match for {method} {path}")


def test_template_catalog_route_is_matched_before_template_id_route() -> None:
    assert first_full_match_path("/api/templates/catalog") == "/api/templates/catalog"
