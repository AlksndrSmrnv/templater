from __future__ import annotations

import logging
import ssl

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.runner import llm_service
from app.routes.deps import SessionDep, TemplatesDep
from app.routes.htmx_utils import form_errors_response, form_str
from app.services.transfer_assistant import TransferAssistant
from app.utils.errors import DomainError, LLMUnavailable

router = APIRouter()
log = logging.getLogger(__name__)

ASSISTANT_ERROR_HEADERS = {"HX-Retarget": "#assistant-errors", "HX-Reswap": "innerHTML"}


def _llm_failure_text(exc: BaseException) -> str:
    if isinstance(exc, (ssl.SSLError, OSError)):
        return (
            "GigaChat недоступен: проверьте GIGACHAT_CERT_B64/GIGACHAT_KEY_B64 в .env. "
            f"Исходная ошибка: {exc}"
        )
    return f"LLM не смогла подобрать перевод: {exc}"


@router.post("/assistant/compose")
async def htmx_compose(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    form = await request.form()
    prompt = form_str(form, "prompt")
    assistant = TransferAssistant(session)
    try:
        async with llm_service(session=session) as llm_svc:
            context = await assistant.compose(prompt, llm_svc)
    except (LLMUnavailable, DomainError) as exc:
        return form_errors_response(
            request,
            templates,
            exc.message,
            details=exc.details,
            status_code=200,
            headers=ASSISTANT_ERROR_HEADERS,
        )
    except (ssl.SSLError, OSError) as exc:
        return form_errors_response(
            request,
            templates,
            _llm_failure_text(exc),
            status_code=200,
            headers=ASSISTANT_ERROR_HEADERS,
        )
    except Exception as exc:  # GigaChat client re-raises a plain Exception on failure
        log.warning("Transfer assistant failed", exc_info=True)
        return form_errors_response(
            request,
            templates,
            _llm_failure_text(exc),
            status_code=200,
            headers=ASSISTANT_ERROR_HEADERS,
        )
    return templates.TemplateResponse(
        request,
        "partials/assistant_result.html",
        context,
    )
