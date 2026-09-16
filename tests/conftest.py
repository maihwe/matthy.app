"""Shared test configuration.

13 §3 is the constraint that shapes this file: **integration tests run against
real PostgreSQL, never SQLite.** There is deliberately no SQLite fallback here.
A fallback would be used the first time PostgreSQL was inconvenient, and the
suite would then pass while the deferred balance trigger and the CHECK
constraints never fired.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from rest_framework.test import APIRequestFactory


@pytest.fixture(autouse=True)
def _clear_correlation_id() -> None:
    """Reset the correlation ID between tests.

    It is a context variable rather than a global, but a test that sets one and
    a later test that asserts on a fresh ID would still interact through the
    same execution context. 13 §5 Y-DET-008: every test is independent.
    """
    from config.logging import set_correlation_id

    set_correlation_id("-")


@pytest.fixture
def api_request_factory() -> APIRequestFactory:
    """DRF's request factory, imported lazily so collection stays cheap."""
    from rest_framework.test import APIRequestFactory

    return APIRequestFactory()
