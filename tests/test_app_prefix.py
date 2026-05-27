from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def test_app_routes_are_registered_under_templater_prefix() -> None:
    app = create_app()
    route_paths = {route.path for route in app.routes}

    assert "/templater/" in route_paths
    assert "/templater/clients" in route_paths
    assert "/templater/templates-htmx/table" in route_paths
    assert "/templater/static" in route_paths
    assert "/" not in route_paths
    assert "/clients" not in route_paths
    assert "/templates-htmx/table" not in route_paths
    assert "/static" not in route_paths


def test_root_and_legacy_static_paths_return_404() -> None:
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        assert client.get("/").status_code == 404
        assert client.get("/clients").status_code == 404
        assert client.get("/static/css/app.css").status_code == 404
        assert client.get("/templater/").status_code == 200
        assert client.get("/templater/static/css/app.css").status_code == 200
