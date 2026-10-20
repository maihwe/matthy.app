"""The ASGI and WSGI entrypoints are importable and expose ``application``.

A deployment failure in these files is invisible to every other test: nothing
else imports them, and the unit under test is one module-level call. Importing
them here means a bad settings path or a missing dependency fails CI rather
than the first deploy (12 §2).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_asgi_exposes_an_application() -> None:
    from config import asgi

    assert callable(asgi.application)


def test_wsgi_exposes_an_application() -> None:
    from config import wsgi

    assert callable(wsgi.application)
