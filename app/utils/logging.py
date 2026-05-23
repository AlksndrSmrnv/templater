from __future__ import annotations

import logging
import logging.config
from typing import Any

import structlog


def _normalize_level(level: str) -> str:
    level_name = level.upper()
    if level_name in logging.getLevelNamesMapping():
        return level_name
    return "INFO"


def _is_pytest_handler(handler: logging.Handler) -> bool:
    return handler.__class__.__module__.startswith("_pytest.")


def configure_logging(level: str, json_output: bool) -> None:
    level_name = _normalize_level(level)
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
    ]
    pre_render_processors: list[Any] = [
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: Any
    if json_output:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.PositionalArgumentsFormatter(),
            *pre_render_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    pytest_handlers = [handler for handler in logging.getLogger().handlers if _is_pytest_handler(handler)]
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "structured": {
                    "()": structlog.stdlib.ProcessorFormatter,
                    "foreign_pre_chain": [*shared_processors, *pre_render_processors],
                    "processors": [
                        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                        renderer,
                    ],
                },
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "structured",
                    "stream": "ext://sys.stdout",
                },
            },
            "root": {
                "handlers": ["default"],
                "level": level_name,
            },
        }
    )

    root_logger = logging.getLogger()
    for handler in pytest_handlers:
        if handler not in root_logger.handlers:
            root_logger.addHandler(handler)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(level_name)
