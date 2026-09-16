"""DEP-LOG-002 — correlation IDs, and the trust boundary on inbound ones.

The inbound header is attacker-controlled. Accepting it verbatim lets a caller
write arbitrary text into the log stream, and for this project the correlation
ID is the key an operator searches by when a member disputes a contribution —
so a forged one does not just add noise, it makes the audit trail unsearchable.
"""

from __future__ import annotations

import pytest
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory

from config.logging import get_correlation_id, new_correlation_id
from config.middleware import CorrelationIdMiddleware

pytestmark = pytest.mark.unit

HEADER = "X-Request-ID"


@pytest.fixture
def factory() -> RequestFactory:
    return RequestFactory()


def _run(factory: RequestFactory, inbound: str | None = None) -> tuple[HttpResponse, HttpRequest]:
    """Send one request through the middleware with an optional inbound header."""
    request = factory.get("/api/v1/anything/")
    if inbound is not None:
        request.META["HTTP_X_REQUEST_ID"] = inbound
    response = CorrelationIdMiddleware(lambda _request: HttpResponse("ok"))(request)
    return response, request


class TestInboundIdIsHonoured:
    """A client or gateway that carries a trace ID keeps it."""

    @pytest.mark.parametrize(
        "inbound",
        [
            "req_deadbeefcafe0001",
            "abc",
            "01HQ2X8K3M-N7P4Q",
            "trace.span:1234",
            "a" * 128,  # exactly the limit
        ],
    )
    def test_a_well_formed_id_is_used(self, factory: RequestFactory, inbound: str) -> None:
        response, request = _run(factory, inbound)
        assert response[HEADER] == inbound
        assert request.correlation_id == inbound
        assert get_correlation_id() == inbound

    def test_the_id_is_echoed_back_to_the_client(self, factory: RequestFactory) -> None:
        """So a user can quote it in a support request and the operator can
        find the exact request."""
        response, _ = _run(factory, "req_cafebabe12345678")
        assert response[HEADER] == "req_cafebabe12345678"


class TestInboundIdIsRejected:
    """Anything else is replaced, not refused.

    A malformed correlation ID is not worth failing a request over — the
    request still needs an ID, and the caller's mistake is not the member's
    problem.
    """

    @pytest.mark.parametrize(
        ("label", "inbound"),
        [
            ("newline", "req_abc\nGET /admin forged"),
            ("carriage return", "req_abc\rforged"),
            ("tab", "req_abc\tforged"),
            ("space", "req abc"),
            ("non-ascii", "req_café"),
            ("too long", "a" * 129),
            ("empty", ""),
            ("sql-ish punctuation", "req_abc'; DROP TABLE users;--"),
            ("braces", "req_{abc}"),
            ("leading hyphen", "-req_abc"),
        ],
    )
    def test_a_malformed_id_is_replaced(
        self, factory: RequestFactory, label: str, inbound: str
    ) -> None:
        response, request = _run(factory, inbound)
        issued = response[HEADER]
        assert issued != inbound, f"{label} should not have been trusted"
        assert issued.startswith("req_")

    def test_a_newline_is_not_ascii_enough(self, factory: RequestFactory) -> None:
        """The specific bug a length-plus-``isascii()`` check leaves open.

        A newline *is* ASCII. Accepting it writes a forged log line, and the
        record's own fields then disagree with the line they are printed on.
        """
        response, _ = _run(factory, "req_ok\n2026-09-16 ERROR payment failed")
        assert "\n" not in response[HEADER]

    def test_a_rejected_id_still_yields_a_usable_one(self, factory: RequestFactory) -> None:
        """Rejection must not leave the request without an ID.

        The failure mode to avoid is a request that 500s in the middleware
        because the header was odd.
        """
        response, request = _run(factory, "\x00\x01\x02")
        assert response.status_code == 200
        assert request.correlation_id
        assert get_correlation_id() == request.correlation_id


class TestMintedIds:
    def test_absent_header_gets_a_minted_id(self, factory: RequestFactory) -> None:
        response, request = _run(factory)
        assert response[HEADER].startswith("req_")
        assert request.correlation_id == response[HEADER]

    def test_minted_ids_are_unique(self, factory: RequestFactory) -> None:
        """Two requests sharing an ID is worse than no ID: it makes two
        unrelated operations look like one."""
        issued = {_run(factory)[0][HEADER] for _ in range(50)}
        assert len(issued) == 50

    def test_the_prefix_matches_the_error_envelope(self) -> None:
        """04 §1.2 shows ``request_id`` as ``req_...``.

        12 §7.1 depends on the two being the same string, so the ID a client
        is shown and the ID in the logs cannot be matched by eye if one is
        prefixed differently from the other.
        """
        assert new_correlation_id().startswith("req_")
        assert len(new_correlation_id()) == len("req_") + 16


class TestContextIsSet:
    def test_the_id_is_visible_to_everything_downstream(self, factory: RequestFactory) -> None:
        """Set before the view runs, so a log line emitted by the view carries
        it. A middleware that only set the response header would leave the
        logs the operator actually reads without an ID."""
        seen: list[str | None] = []

        request = factory.get("/api/v1/anything/")
        request.META["HTTP_X_REQUEST_ID"] = "req_0123456789abcdef"

        def view(_request: object) -> HttpResponse:
            seen.append(get_correlation_id())
            return HttpResponse("ok")

        CorrelationIdMiddleware(view)(request)
        assert seen == ["req_0123456789abcdef"]

    def test_a_celery_task_inherits_the_id(self) -> None:
        """DEP-LOG-002 — the ID must cross the process boundary.

        ``contextvars`` is copied into a Celery task's execution context, which
        is the property that makes one logical operation traceable from the
        HTTP request through to the payment provider call.
        """
        import contextvars

        set_correlation_id("req_ffee00112233aabb")
        copied = contextvars.copy_context()
        set_correlation_id("-")
        assert copied.run(get_correlation_id) == "req_ffee00112233aabb"
