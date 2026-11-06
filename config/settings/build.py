"""Collect static assets without runtime secrets or database/cache services.

Used only during image construction. SimpleAdminConfig avoids importing the
project's admin registrations while Django discovers the bundled admin assets.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEBUG = False
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.admin.apps.SimpleAdminConfig",
    "django.contrib.staticfiles",
]
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
