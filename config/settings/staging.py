"""Staging settings.

12 §1: *"A staging environment with different instance sizes, a different
PostgreSQL major version, or no Redis is not a rehearsal — it is a different
system that happens to run the same code, and it will pass changes that
production rejects."*

So staging **derives from production** rather than restating it. The only
differences permitted are the ones a downscaled-but-not-simplified environment
genuinely requires: a smaller log level, and a distinct secret set supplied by
the environment (SEC-SEC-005).

A hardening setting added to production is therefore in force here on the next
deploy, with no second file to remember. That is the entire point of the
import.
"""

from __future__ import annotations

from config.env import env
from config.settings.production import *  # noqa: F403

# Staging runs with real-shaped data but never real people's data, and its
# secrets are its own: a staging credential cannot open production.
LOGGING["root"]["level"] = env("DJANGO_LOG_LEVEL", default="DEBUG")  # noqa: F405
