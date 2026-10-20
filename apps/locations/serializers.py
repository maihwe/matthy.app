"""Serializers for ``locations`` — 04 §4.

Every serializer lists its fields explicitly (10 §11 S5). The public identifier
is exposed as ``id`` and the internal integer key never is (04 §1): a client
that holds a sequential key can enumerate the table, and a key that appears in a
URL appears in access logs, screenshots and bug reports.

All four serializers are read-only in this slice. Location administration is
``platform.manage_locations`` (04 §4), and that permission is part of S5's
catalogue, so the write methods arrive with the permission that guards them
rather than with a temporary weaker check.
"""

from __future__ import annotations

from typing import Any, ClassVar

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.locations.constants import MAX_EMBEDDED_CHILDREN
from apps.locations.models import Country, Location, LocationType

__all__ = [
    "CountrySerializer",
    "LocationDetailSerializer",
    "LocationSerializer",
    "LocationSummarySerializer",
    "LocationTypeSerializer",
]


class CountrySerializer(serializers.ModelSerializer[Country]):
    """A country and the currency it uses.

    ``currency`` is a two-way field in the response: the code, not a nested
    object, because the caller's next request is ``?country=NG`` and not a web
    of joins.
    """

    currency = serializers.CharField(source="currency_id", read_only=True)

    class Meta:
        model = Country
        fields = (
            "iso2",
            "iso3",
            "name",
            "dial_code",
            "currency",
            "default_locale",
        )


class LocationTypeSerializer(serializers.ModelSerializer[LocationType]):
    """One administrative tier as the country defines it (FR-LOC-001)."""

    class Meta:
        model = LocationType
        fields = (
            "code",
            "name_singular",
            "name_plural",
            "level",
            "is_leaf",
        )


class LocationSummarySerializer(serializers.ModelSerializer[Location]):
    """The child shape in a location detail response.

    Deliberately narrow: a state with 774 LGAs must not make the state's detail
    response carry every LGA's coordinates.
    """

    id = serializers.UUIDField(source="public_id", read_only=True)
    type = serializers.CharField(source="location_type.code", read_only=True)
    level = serializers.IntegerField(source="location_type.level", read_only=True)
    is_leaf = serializers.BooleanField(source="location_type.is_leaf", read_only=True)

    class Meta:
        model = Location
        fields = ("id", "name", "slug", "type", "level", "is_leaf")


class LocationSerializer(serializers.ModelSerializer[Location]):
    """A location in a list or search result (04 §4)."""

    id = serializers.UUIDField(source="public_id", read_only=True)
    type = serializers.CharField(source="location_type.code", read_only=True)
    level = serializers.IntegerField(source="location_type.level", read_only=True)
    is_leaf = serializers.BooleanField(source="location_type.is_leaf", read_only=True)
    country = serializers.CharField(source="country.iso2", read_only=True)
    parent_id = serializers.UUIDField(
        source="parent.public_id",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = Location
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "name",
            "slug",
            "code",
            "type",
            "level",
            "is_leaf",
            "country",
            "parent_id",
            "latitude",
            "longitude",
        )


class LocationDetailSerializer(LocationSerializer):
    """A location with its direct children.

    ``group_count`` and ``/locations/{id}/groups/`` (FR-LOC-004) arrive with
    S5: they read ``groups``, which is L1 and does not exist yet. Returning a
    hard-coded zero here would be a field a client could rely on and that would
    be wrong — worse than a field that arrives documented and late.
    """

    children = serializers.SerializerMethodField()

    class Meta(LocationSerializer.Meta):
        fields = (*LocationSerializer.Meta.fields, "children")

    @extend_schema_field(LocationSummarySerializer(many=True))
    def get_children(self, obj: Location) -> Any:
        """The bounded child window described on the view.

        ``active_children`` is what ``LocationDetailView`` prefetched; the
        attribute is absent when the serializer is used elsewhere, so the
        fallback is the same query with the same bound. Slicing an already
        fetched list costs nothing and keeps the response cap in one place.
        """
        prefetched = getattr(obj, "active_children", None)
        if prefetched is None:
            prefetched = list(
                obj.children.filter(is_active=True)
                .select_related("location_type")
                .order_by("name")[:MAX_EMBEDDED_CHILDREN]
            )
        return LocationSummarySerializer(prefetched[:MAX_EMBEDDED_CHILDREN], many=True).data
