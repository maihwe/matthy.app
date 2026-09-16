"""ASGI entrypoint.

ASGI rather than WSGI because long-lived connections are coming: notification
delivery and any future streaming of report generation both outlive a
request/response cycle. The process role is chosen by the entrypoint
(12 §2), not by this file.
"""

from __future__ import annotations

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

application = get_asgi_application()
