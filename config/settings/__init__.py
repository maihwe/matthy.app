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
"""
