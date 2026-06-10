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


class SettingsLockedError(DomainError):
    """Raised when a mutating settings endpoint is hit without edit mode."""

    status_code = 403
    code = "settings_locked"


class ValidationFailed(DomainError):
    status_code = 422
    code = "validation_failed"


class ContentParseFailed(ValidationFailed):
    """Body does not parse as the template's declared format.

    Carries the 1-based line/column of the parse error so the editor UI can
    highlight the offending line.
    """

    code = "content_parse_failed"

    def __init__(self, message: str, *, line: int | None = None, col: int | None = None) -> None:
        details = [f"Строка {line}, позиция {col}"] if line is not None else None
        super().__init__(message, details=details)
        self.line = line
        self.col = col


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
