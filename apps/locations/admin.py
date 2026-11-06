"""Django admin registration for ``locations``.

Locations are administratively managed (FR-LOC-005) — ordinary members cannot
create a top-tier place — and the admin is that management surface in Phase 1.
S5 wraps these registrations with read-only, ``platform.manage_locations``-guarded
inspection in L1. Audited mutations now use the location API/services.

The admin form calls ``Location.clean()``, which is where the tree rules are
expressed legibly. The database trigger still refuses an inconsistent row
written by anything else; the two are not redundant, they are the same rule
stated once for a person and once for the database.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from django.contrib import admin

from apps.locations.models import Country, Currency, Location, LocationType


@admin.register(Currency)
class CurrencyAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "numeric_code",
        "name",
        "minor_units",
        "rounding_mode",
        "is_active",
    )
    list_filter = ("is_active", "rounding_mode")
    search_fields = ("code", "name")


@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    list_display = ("iso2", "iso3", "name", "dial_code", "currency", "is_active")
    list_filter = ("is_active", "currency")
    search_fields = ("iso2", "iso3", "name")


@admin.register(LocationType)
class LocationTypeAdmin(admin.ModelAdmin):
    list_display = ("country", "code", "name_singular", "name_plural", "level", "is_leaf")
    list_filter = ("country", "is_leaf")
    ordering = ("country", "level")


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("name", "location_type", "parent", "country", "is_active")
    list_filter = ("country", "location_type", "is_active")
    search_fields = ("name", "slug", "code", "public_id")
    # The tree is large and the parent is chosen by browsing, not by scrolling a
    # `<select>` of every location in the country.
    raw_id_fields = ("parent",)
    readonly_fields = ("public_id", "created_at", "updated_at")
    # Typing a name should not be the only way to produce a slug, but silently
    # rewriting a reviewer's input is worse than asking. The field stays
    # editable and the constraint in `Meta` states the required format.
    prepopulated_fields: ClassVar[dict[str, Sequence[str]]] = {"slug": ("name",)}
