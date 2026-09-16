"""What ``record()`` records about *where* an action came from — FR-AUD-001.

The whole file is about one header. ``X-Forwarded-For`` is client-supplied, and
a proxy that forwards it without rewriting it is a proxy the client can lie
through; writing that lie into an append-only record is worse than writing
nothing, because the field would then be trusted precisely because it is
immutable.

No database: ``AuditContext`` is a dataclass and ``client_ip`` reads a dict.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from django.test import RequestFactory

from apps.audit.services import AuditContext, client_ip

pytestmark = pytest.mark.unit


@pytest.fixture
def request_factory() -> RequestFactory:
    return RequestFactory()


class TestClientIp:
    def test_reads_remote_addr(self, request_factory: RequestFactory) -> None:
        request = request_factory.get("/", REMOTE_ADDR="203.0.113.7")
        assert client_ip(request) == "203.0.113.7"

    def test_ignores_x_forwarded_for(self, request_factory: RequestFactory) -> None:
        """The security property, stated as a test.

        A client can send ``X-Forwarded-For`` itself. Behind a proxy that passes
        the header through, the value is attacker-chosen — and because audit rows
        are immutable, a forged one is forged permanently. Until 12 specifies how
        many proxies sit in front of the app, the only safe source is the socket
        peer, which is never wrong even when it is less useful.

        This test is expected to change when that is decided, and the replacement
        must read the header only from a position the client cannot reach.
        """
        request = request_factory.get(
            "/",
            REMOTE_ADDR="203.0.113.7",
            HTTP_X_FORWARDED_FOR="198.51.100.9, 10.0.0.1",
        )
        assert client_ip(request) == "203.0.113.7"

    def test_absent_remote_addr_is_none_not_empty_string(self) -> None:
        """An empty string in the column would look like a recorded value."""

        class _Bare:
            META: dict[str, str] = {}

        assert client_ip(_Bare()) is None  # type: ignore[arg-type]


class TestAuditContextFromRequest:
    def test_captures_ip_and_user_agent(self, request_factory: RequestFactory) -> None:
        request = request_factory.get(
            "/",
            REMOTE_ADDR="203.0.113.7",
            HTTP_USER_AGENT="ihweza-android/1.0",
        )
        context = AuditContext.from_request(request)
        assert context.ip_address == "203.0.113.7"
        assert context.user_agent == "ihweza-android/1.0"

    def test_a_missing_user_agent_is_none(self, request_factory: RequestFactory) -> None:
        request = request_factory.get("/", REMOTE_ADDR="203.0.113.7")
        assert AuditContext.from_request(request).user_agent is None

    def test_does_not_read_device_or_session_off_the_request(
        self, request_factory: RequestFactory
    ) -> None:
        """Named attributes that happen to look right are not enough.

        The request attribute names for the current device and session are the
        authentication layer's to define (S4). Reading them here by guess would
        put a silent ``None`` into exactly the column an investigation uses to ask
        "had they already revoked that device?", and it would look like a recorded
        answer rather than an absent one.
        """
        request = request_factory.get("/", REMOTE_ADDR="203.0.113.7")
        request.device_id = 4242  # type: ignore[attr-defined]
        request.session_id = 99  # type: ignore[attr-defined]
        context = AuditContext.from_request(request)
        assert context.device_id is None
        assert context.session_id is None

    def test_context_is_frozen(self) -> None:
        """A context that could be mutated after the fact is a record that can be
        edited on its way to the table."""
        context = AuditContext(ip_address="203.0.113.7")
        with pytest.raises(FrozenInstanceError):
            context.ip_address = "198.51.100.9"  # type: ignore[misc]
