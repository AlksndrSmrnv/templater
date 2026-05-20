from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for application-level errors that should be rendered nicely to the user."""

    status_code: int = 400
    code: str = "domain_error"

    def __init__(self, message: str, *, details: Any | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class ValidationFailed(DomainError):
    status_code = 422
    code = "validation_failed"


class IntegrityViolation(DomainError):
    """Raised when an operation would violate referential integrity rules."""

    status_code = 409
    code = "integrity_violation"


class LLMUnavailable(DomainError):
    status_code = 503
    code = "llm_unavailable"


class LLMResponseError(DomainError):
    status_code = 502
    code = "llm_response_error"
