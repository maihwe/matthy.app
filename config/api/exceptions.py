"""The single error envelope — 04 §1.2.

Every error the API returns, from any layer, has this shape:

    {"error": {"code", "message", "details", "request_id"}}

The uniformity is the point: a client's error interceptor is written once, and
a new endpoint cannot invent a second dialect.

Two rules here are load-bearing rather than cosmetic:

* **``404 not_found`` and ``403 insufficient_permission`` are deliberately
  ambiguous** for resources the caller has no standing in (04 §1.2). Revealing
  that a group exists to someone with no access to it is an information leak.
* **``500`` carries no internals.** Stack traces, SQL fragments and framework
  versions never reach a client (10 §5.2).

The ``code`` is never taken from DRF's own ``default_code``. DRF reports a
``ValidationError`` as ``"invalid"`` and a ``NotFound`` as ``"not_found"``, and
only the latter happens to match the contract — so the mapping is explicit and
keyed on the status, with the project's own exceptions overriding it.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from django.core.exceptions import PermissionDenied
from rest_framework import exceptions as drf
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from config.logging import get_correlation_id

__all__ = [
    "APIError",
    "Conflict",
    "IdempotencyConflict",
    "SeparationOfDuties",
    "Unprocessable",
    "envelope_exception_handler",
]

logger = logging.getLogger(__name__)


class APIError(drf.APIException):
    """Base for this project's own error codes.

    Subclasses set ``status_code`` and ``api_code``. ``api_code`` is a separate
    attribute rather than a reuse of DRF's ``default_code`` because the two mean
    different things: ``default_code`` is DRF's internal label, ``api_code`` is
    the string a client is promised in the envelope.
    """

    api_code: str = "internal_error"
    default_detail: str = "An error occurred."

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.default_detail)


class Conflict(APIError):
    """409 — a state conflict: duplicate reference, invalid transition."""

    status_code: Final = 409
    api_code: Final = "conflict"
    default_detail: Final = "The request conflicts with the resource's current state."


class IdempotencyConflict(APIError):
    """409 — a key reused with a different payload (04 §1.5).

    That is a client bug, and surfacing it is more useful than honouring
    whichever version arrived second.
    """

    status_code: Final = 409
    api_code: Final = "idempotency_conflict"
    default_detail: Final = "This idempotency key was used with a different request body."


class Unprocessable(APIError):
    """422 — well-formed but violates a business rule."""

    status_code: Final = 422
    api_code: Final = "unprocessable"
    default_detail: Final = "The request is well-formed but cannot be applied."


class SeparationOfDuties(PermissionDenied):
    """403 with its own code — the one 403 a client should never retry.

    No permission change will make a self-approval succeed (05 §6, §44 rule 6),
    so offering the client a retry would be a lie.

    Subclasses Django's ``PermissionDenied`` rather than DRF's, so that the
    service layer can raise it without importing DRF. DRF's own handler
    converts it on the way through, and the original exception is still in hand
    here when the code is chosen.
    """

    api_code: Final = "separation_of_duties"
    default_detail: Final = "This action cannot be performed by the requester."

    def __str__(self) -> str:
        return super().__str__() or self.default_detail


_STATUS_TO_CODE: Final[dict[int, str]] = {
    400: "validation_error",
    401: "authentication_required",
    403: "insufficient_permission",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    415: "unsupported_media_type",
    422: "unprocessable",
    429: "rate_limited",
}

_FALLBACK_MESSAGES: Final[dict[str, str]] = {
    "validation_error": "The request was malformed or contained invalid values.",
    "authentication_required": "Authentication credentials were not provided or are invalid.",
    "insufficient_permission": "You do not have permission to perform this action.",
    "not_found": "Not found.",
    "internal_error": "An internal error occurred. It has been logged.",
}

_500_MESSAGE: Final = _FALLBACK_MESSAGES["internal_error"]


def _details_from(exc: Exception) -> list[dict[str, Any]]:
    """Flatten a DRF validation error into ``[{field, issue}]``.

    The envelope's ``details`` shape is fixed by 04 §1.2, so DRF's nested
    dict-of-lists is flattened here. A client should not have to know that DRF
    nests errors one level per serializer field.
    """
    if not isinstance(exc, drf.ValidationError):
        return []

    details: list[dict[str, Any]] = []

    def walk(value: Any, field: str | None) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, str(key) if field is None else f"{field}.{key}")
        elif isinstance(value, list | tuple):
            for item in value:
                walk(item, field)
        elif isinstance(value, list | tuple) is False:
            details.append({"field": field or "non_field_errors", "issue": str(value)})

    walk(exc.detail, None)
    return details


def _code_for(exc: Exception, status: int) -> str:
    """Choose the envelope's ``code``.

    Status-keyed rather than taken from DRF, because DRF's own codes disagree
    with 04 §1.2 in places — most importantly ``ValidationError``, whose
    ``default_code`` is ``"invalid"`` while the contract promises
    ``"validation_error"``.
    """
    if isinstance(exc, SeparationOfDuties):
        return SeparationOfDuties.api_code
    if isinstance(exc, drf.ValidationError):
        return "validation_error"
    if isinstance(exc, APIError):
        return exc.api_code
    return _STATUS_TO_CODE.get(status, "internal_error")


def _message_for(exc: Exception, code: str) -> str:
    """Choose the envelope's ``message``.

    Prefers the exception's own detail when it is a plain string, so a specific
    message survives rather than being flattened to a generic one.
    """
    if isinstance(exc, SeparationOfDuties):
        return str(exc) or SeparationOfDuties.default_detail

    if isinstance(exc, drf.APIException):
        detail = exc.detail
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list) and detail and all(isinstance(d, str) for d in detail):
            return "; ".join(str(d) for d in detail)

    return _FALLBACK_MESSAGES.get(code, _FALLBACK_MESSAGES["internal_error"])


def _envelope(
    code: str, message: str, details: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or [],
            "request_id": get_correlation_id() or "-",
        }
    }


def envelope_exception_handler(exc: Exception, context: dict[str, Any]) -> Response:
    """DRF's exception handler, returning 04 §1.2's envelope.

    Always returns a response. An exception DRF does not recognise is an
    unhandled failure, and it still has the documented shape — with no
    internals in it (10 §5.2).
    """
    response = drf_exception_handler(exc, context)

    if response is None:
        logger.exception("Unhandled exception in API view")
        return Response(_envelope("internal_error", _500_MESSAGE), status=500)

    status = response.status_code
    code = _code_for(exc, status)
    response.data = _envelope(code, _message_for(exc, code), _details_from(exc))

    if status == 429:
        # 04 §1.2: 429 includes Retry-After.
        wait = getattr(exc, "wait", None)
        if wait is not None:
            response["Retry-After"] = str(int(wait))

    if status >= 500:
        logger.error("API error %s: %s", code, exc, exc_info=exc)

    return response
