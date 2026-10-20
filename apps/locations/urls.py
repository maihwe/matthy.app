"""URL configuration for ``locations`` — 04 §4.

Included under ``/api/v1/locations/`` by ``config.urls``. The country code in
the path is ISO 3166-1 alpha-2; the location identifier is the public UUID,
never the integer key (04 §1).
"""

from __future__ import annotations

from django.urls import URLPattern, path

from apps.locations.views import (
    CountryListView,
    CountryTypeListView,
    LocationDetailView,
    LocationListView,
)

app_name = "locations"

urlpatterns: list[URLPattern] = [
    path("countries/", CountryListView.as_view(), name="country-list"),
    path(
        "countries/<str:code>/types/",
        CountryTypeListView.as_view(),
        name="country-types",
    ),
    path("", LocationListView.as_view(), name="location-list"),
    path("<uuid:public_id>/", LocationDetailView.as_view(), name="location-detail"),
]
