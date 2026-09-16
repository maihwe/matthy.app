"""Structured logging and correlation IDs — DEP-LOG-001, DEP-LOG-002, DEP-LOG-003.

DEP-LOG-002 is what makes the rest usable: without a correlation ID, an HTTP
request that enqueues a Celery task that calls a payment provider produces
three unrelated log streams, and debugging a failed payment becomes
archaeology. The ID is carried in a context variable, which Celery's task
primitives propagate, so one logical operation stays traceable across
processes.

DEP-LOG-003 is the rule this module's redaction exists to support — but
redaction is defence in depth, not the control. The control is not logging the
credential in the first place, and the security suite asserts that (10 §11, S1).
"""

from __future__ import annotations

import json
import logging
import secrets
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Final

__all__ = [
    "CorrelationIdFilter",
    "JsonFormatter",
    "get_correlation_id",
    "new_correlation_id",
    "set_correlation_id",
]

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

REDACTED: Final = "[REDACTED]"
"""DEP-LOG-003: log records never carry these. Matched case-insensitively as a
substring of the key, so ``refresh_token`` and ``pin`` are both caught."""

REDACTED_KEYS: Final = frozenset(
    {
        "password",
        "passwd",
        "pin",
        "pin_hash",
        "token",
        "access_token",
        "refresh_token",
        "mfa_token",
        "authorization",
        "api_key",
        "secret",
        "otp",
        "code",  # OTP codes and verification codes
        "card_number",
        "cvv",
        "account_number",
    }
)

_LOG_RECORD_RESERVED: Final = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def new_correlation_id() -> str:
    """Mint a correlation ID.

    The ``req_`` prefix matches the ``request_id`` shown in the error envelope
    of 04 §1.2, so the ID a client is shown is the ID its logs carry.
    """
    return f"req_{secrets.token_hex(8)}"


def set_correlation_id(value: str) -> None:
    _correlation_id.set(value)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def _redact(value: Any, *, depth: int = 0) -> Any:
    """Recursively replace credential-shaped values.

    Bounded by ``depth`` so that a self-referential structure cannot recurse
    without end and take the logger down with it — a failure mode that would
    turn a logging problem into an outage during exactly the incident the logs
    were needed for.
    """
    if depth > 4:
        return "..."
    if isinstance(value, dict):
        return {
            key: (REDACTED if _is_sensitive(key) else _redact(item, depth=depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact(item, depth=depth + 1) for item in value]
    return value


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in REDACTED_KEYS)


class CorrelationIdFilter(logging.Filter):
    """Attaches the current correlation ID to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """Renders a record as a single JSON object.

    One line per record, because a multi-line record is unparseable by every
    log shipper worth using, and the traceback is the field that matters most
    when something has gone wrong.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", "-"),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Anything passed via `extra=` lands under "context", redacted. This is
        # how 12 §7.1's DEP-LOG-004 is satisfied: a financial operation logs its
        # reference, amount and outcome, without the actor's credentials.
        context = {
            key: _redact(value)
            for key, value in record.__dict__.items()
            if key not in _LOG_RECORD_RESERVED and not key.startswith("_")
        }
        context.pop("correlation_id", None)
        if context:
            payload["context"] = context

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)
