"""WSGI entrypoint.

Retained alongside ASGI because some deployment platforms and tooling still
expect it, and because the Celery workers and management commands do not care
which is configured.
"""

from __future__ import annotations

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

application = get_wsgi_application()
