"""Settings package.

Each environment module imports ``*`` from ``base`` and states only its
differences. Select one with ``DJANGO_SETTINGS_MODULE``:

    config.settings.dev         local work, Docker Compose (02 §8)
    config.settings.test        automated test runs, ephemeral database
    config.settings.staging     pre-production verification
    config.settings.production  live

There is no ``local.py`` and no import-time fallback between these. 12 §1:
production is never a development target, and the way to guarantee that is to
make the wrong choice impossible rather than discouraged.

Each environment module follows two import rules, both of which exist to keep a
real linter diagnostic working:

* Environment helpers come from ``config.env`` directly, never through the star
  import. Reaching them via ``base`` works at runtime — it is the same object —
  but leaves the name star-only, which forces a ``# noqa: F405``.
* A name taken from the star import (``LOGGING``, ``DATABASES``, ``CACHES``)
  carries a ``# noqa: F405``, and that comment should cover exactly one name.
  A single ``noqa`` covering two is how a genuine ``NameError`` gets hidden.
"""
