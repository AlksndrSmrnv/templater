from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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
    collections,
    entities_htmx,
    export_import,
    filled_templates,
    home,
    references,
    send,
    templates_reg,
)
from app.routes import (
    settings as settings_routes,
)
from app.utils.errors import DomainError
from app.utils.logging import configure_logging

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await shutdown_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    app = FastAPI(title="Template Maker", debug=settings.app_debug, lifespan=lifespan)
    app_prefix = "/templater"

    app.mount(f"{app_prefix}/static", StaticFiles(directory=settings.static_dir), name="static")

    app.include_router(home.router, prefix=app_prefix)
    app.include_router(clients.router, prefix=app_prefix)
    app.include_router(accounts.router, prefix=app_prefix)
    app.include_router(cards.router, prefix=app_prefix)
    app.include_router(entities_htmx.router, prefix=app_prefix)
    app.include_router(references.router, prefix=app_prefix)
    app.include_router(templates_reg.router, prefix=app_prefix)
    app.include_router(collections.router, prefix=app_prefix)
    app.include_router(filled_templates.router, prefix=app_prefix)
    app.include_router(send.router, prefix=app_prefix)
    app.include_router(settings_routes.router, prefix=app_prefix)
    app.include_router(export_import.router, prefix=app_prefix)

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

    @app.exception_handler(Exception)
    async def _unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        log.exception(
            "Unhandled exception while handling %s %s",
            request.method,
            request.url.path,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_server_error",
                "message": "Внутренняя ошибка сервера",
            },
        )

    return app


app = create_app()
