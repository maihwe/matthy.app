"""The Django project package.

Importing the Celery app here means ``@shared_task`` is bound to this app
without every module having to import Celery itself.
"""

from __future__ import annotations

from config.celery import app as celery_app

__all__ = ("celery_app",)
