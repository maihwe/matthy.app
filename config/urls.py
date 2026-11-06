"""URL configuration.

Paths are fixed by 04 §1: the API is versioned under ``/api/v1/``, and the
OpenAPI schema is generated from the code rather than hand-maintained, so it
cannot drift from the implementation (02 §6).

The API version moves independently of the application version and changes
only on a **breaking** contract change (11 §7, 04 §1.1).
"""

from __future__ import annotations

from django.contrib import admin
from django.urls import URLPattern, URLResolver, include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.groups import views as group_views
from config import branding
from config.health import liveness, readiness

admin.site.site_header = f"{branding.BRAND_NAME} administration"
admin.site.site_title = branding.BRAND_NAME
admin.site.index_title = f"{branding.BRAND_NAME} — {branding.BRAND_TAGLINE}"

# Project apps are included here by the slice that builds them. An endpoint
# added without its permission class is a test failure (SEC-AUTHZ-001), not
# this file's problem.
api_v1_patterns: list[URLPattern | URLResolver] = [
    path("auth/", include("apps.accounts.urls")),
    path("groups/", include("apps.groups.urls")),
    path("platform/roles/", group_views.PlatformRolesView.as_view(), name="platform-roles"),
    path(
        "platform/roles/<uuid:role_id>/permissions/",
        group_views.PlatformRolePermissionsView.as_view(),
        name="platform-role-permissions",
    ),
    path(
        "platform/users/<uuid:user_id>/roles/<uuid:role_id>/",
        group_views.PlatformRoleRevokeView.as_view(),
        name="platform-role-revoke",
    ),
    path("group-types/", group_views.GroupTypesView.as_view(), name="group-types"),
    path(
        "group-types/<str:code>/",
        group_views.GroupTypeDetailView.as_view(),
        name="group-type-detail",
    ),
    path("role-templates/", group_views.RoleTemplatesView.as_view(), name="role-templates"),
    path(
        "permissions/", group_views.PermissionCatalogueView.as_view(), name="permission-catalogue"
    ),
    path(
        "locations/<uuid:public_id>/groups/",
        group_views.LocationGroupsView.as_view(),
        name="location-groups",
    ),
    path(
        "locations/<uuid:public_id>/",
        group_views.GroupLocationDetailView.as_view(),
        name="location-detail-groups",
    ),
    path("locations/", group_views.GroupLocationListView.as_view(), name="location-list-platform"),
    # L1 wraps the public location reads with permission-guarded platform writes.
    path("locations/", include("apps.locations.urls")),
]

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
