"""Development settings — the builder's machine, synthetic data (02 §8, 12 §1).

Assumes the Docker Compose stack is up, so the environment variables
``base`` requires are already present. There is no fallback for a missing
secret here either: a development machine that boots without one is a machine
that will deploy without one.
"""

from __future__ import annotations

from config.env import env_int
from config.settings.base import *  # noqa: F403
from config.settings.base import env, env_list

DEBUG = True

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "[::1]"])

# Email goes to Mailhog in Compose, so nothing leaves the machine during
# development. 13 §11 — no outbound network, and none needed here.
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", default="localhost")
EMAIL_PORT = env_int("EMAIL_PORT", default=1025)
EMAIL_USE_TLS = False

INTERNAL_IPS = ["127.0.0.1"]
