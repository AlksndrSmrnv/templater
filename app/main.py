from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db.session import shutdown_engine
from app.routes import (
    accounts,
    cards,
    clients,
    export_import,
    home,
    references,
    send,
    templates_reg,
)
from app.routes import (
    settings as settings_routes,
)
from app.utils.errors import DomainError


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Template Maker", debug=settings.app_debug)

    app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")

    app.include_router(home.router)
    app.include_router(clients.router)
    app.include_router(accounts.router)
    app.include_router(cards.router)
    app.include_router(references.router)
    app.include_router(templates_reg.router)
    app.include_router(send.router)
    app.include_router(settings_routes.router)
    app.include_router(export_import.router)

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, "message": exc.message, "details": exc.details},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": "validation_error", "details": exc.errors()},
        )

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:
        await shutdown_engine()

    return app


app = create_app()
