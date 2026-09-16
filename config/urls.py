"""URL configuration.

Paths are fixed by 04 §1: the API is versioned under ``/api/v1/``, and the
OpenAPI schema is generated from the code rather than hand-maintained, so it
cannot drift from the implementation (02 §6).

The API version moves independently of the application version and changes
only on a **breaking** contract change (11 §7, 04 §1.1).
"""

from __future__ import annotations

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from config import branding
from config.health import liveness, readiness

admin.site.site_header = f"{branding.BRAND_NAME} administration"
admin.site.site_title = branding.BRAND_NAME
admin.site.index_title = f"{branding.BRAND_NAME} — {branding.BRAND_TAGLINE}"

# Project apps are included here by the slice that builds them. An empty list
# is correct in S0: there is no endpoint yet, and an endpoint added without its
# permission class is a test failure (SEC-AUTHZ-001), not this file's problem.
api_v1_patterns: list[object] = []

urlpatterns = [
    path("admin/", admin.site.urls),
    # Health is unversioned and unauthenticated on purpose: a load balancer
    # must be able to reach it without credentials (12 §6).
    path("health/live/", liveness, name="health-live"),
    path("health/ready/", readiness, name="health-ready"),
    path("api/v1/", include((api_v1_patterns, "v1"))),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
]
