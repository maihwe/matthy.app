"""Project middleware."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final

from django.http import HttpRequest, HttpResponse

from config.logging import new_correlation_id, set_correlation_id

__all__ = ["CorrelationIdMiddleware"]

_HEADER: Final = "X-Request-ID"

_MAX_INBOUND_LENGTH: Final = 128

# An inbound correlation ID is accepted only if it matches this exactly.
#
# A whitelist, not a blacklist, and not a length-plus-`isascii()` check: a
# newline *is* ASCII, so `"req\nGET /admin forged"` would have passed a
# character-class check and been written into every log line the request
# produced. That is a log-poisoning attack, and for this project it is worse
# than a cosmetic problem — the correlation ID is what an operator uses to
# find a disputed financial operation in the audit trail (12 §7.1).
_PATTERN: Final = re.compile(rf"\A[A-Za-z0-9._:-]{{1,{_MAX_INBOUND_LENGTH}}}\Z")


class CorrelationIdMiddleware:
    """Assigns each request a correlation ID and echoes it back (DEP-LOG-002).

    An inbound ``X-Request-ID`` is honoured when it is short and opaque, so
    that a mobile client or an upstream gateway can carry one trace across
    services. Anything else is replaced with a freshly minted ID rather than
    rejected: a malformed correlation ID is not worth failing a request over,
    and the request still needs one.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        inbound = request.headers.get(_HEADER, "")
        correlation_id = inbound if _PATTERN.match(inbound) else new_correlation_id()

        set_correlation_id(correlation_id)
        request.correlation_id = correlation_id  # type: ignore[attr-defined]

        response = self.get_response(request)
        response[_HEADER] = correlation_id
        return response
