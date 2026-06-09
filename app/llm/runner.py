"""Glue to wire the GigaChat client into the application pipeline.

The function below follows the structure described in the integration spec
(cert decoding → client construction → service usage → ``finally`` cleanup).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from app.config import Settings, get_settings
from app.llm.certs import remove_temp_file, resolve_cert_files
from app.llm.client import GigaChatClient
from app.llm.coordinator import get_coordinator
from app.llm.prompts import load_prompt_overrides
from app.llm.service import LLMService
from app.utils.errors import LLMUnavailable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


@asynccontextmanager
async def llm_service(
    settings: Settings | None = None,
    *,
    session: "AsyncSession | None" = None,
) -> AsyncIterator[LLMService]:
    s = settings or get_settings()
    if not s.llm_active:
        raise LLMUnavailable("LLM не настроена (нет URL или сертификатов в .env)")

    # Load editable prompt overrides from the DB so edits made in settings apply
    # to the very next analysis run without an app restart. Without a session we
    # fall back to the coded defaults (keeps the manager usable in scripts/tests).
    prompt_overrides = await load_prompt_overrides(session) if session is not None else None

    cert_path = None
    key_path = None
    client: GigaChatClient | None = None
    try:
        cert_path, key_path = resolve_cert_files(s.gigachat_cert_b64, s.gigachat_key_b64)
        coordinator = get_coordinator(concurrency=s.llm_concurrency, min_delay=s.llm_request_delay)
        client = GigaChatClient(
            base_url=s.gigachat_base_url,
            cert_file=cert_path,
            key_file=key_path,
            model=s.gigachat_model,
            timeout=s.llm_timeout,
            max_retries=s.llm_max_retries,
            retry_base_delay=s.llm_retry_base_delay,
            coordinator=coordinator,
        )
        yield LLMService(client, coordinator=coordinator, prompt_overrides=prompt_overrides)
    finally:
        if client is not None:
            await client.close()
        remove_temp_file(cert_path)
        remove_temp_file(key_path)
