"""Location read endpoints — 04 §4, FR-LOC-002…005.

These are public reads: finding a street is part of the ordinary member
journey, and requiring authentication for reference data would mean a member
cannot see where a group is before signing in. They are not unthrottled,
though: ``AnonymousReadThrottle`` applies 10 §6.4's ``anonymous_read`` scope.

Every view declares ``permission_classes`` explicitly. The static ratchet in
``tests/security/test_static_guarantees.py`` fails the build otherwise, and
that is the point — ``AllowAny`` is a decision here, not an omission.

L1 group views wrap these public reads with ``platform.manage_locations`` writes
at the project router. Keeping that wrapper in L1 preserves the layer dependency.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.db.models import Prefetch, QuerySet
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import exceptions as drf
from rest_framework import generics
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.locations.models import Country, Location, LocationType
from apps.locations.serializers import (
    CountrySerializer,
    LocationDetailSerializer,
    LocationSerializer,
    LocationTypeSerializer,
)
from config.api.pagination import data_response
from config.api.throttling import AnonymousReadThrottle

__all__ = [
    "CountryListView",
    "CountryTypeListView",
    "LocationDetailView",
    "LocationListView",
]

# How many children a detail response embeds lives in ``constants`` so the
# serializer can apply the same bound when it is used on its own.


class _PublicReadView(APIView):
    """Shared declaration for the public location reads."""

    permission_classes = (AllowAny,)
    throttle_classes = (AnonymousReadThrottle,)


class CountryListView(generics.ListAPIView[Country]):
    """``GET /locations/countries/`` — active countries, by name."""

    permission_classes = (AllowAny,)
    throttle_classes = (AnonymousReadThrottle,)
    serializer_class = CountrySerializer
    # Cursor pagination requires a deterministic, total order. `name` alone is
    # not unique; `iso2` makes the ordering total, so a page boundary is a
    # position rather than a suggestion.
    ordering: ClassVar[tuple[str, ...]] = ("name", "iso2")

    def get_queryset(self) -> QuerySet[Country]:
        return Country.objects.filter(is_active=True).select_related("currency")


class CountryTypeListView(_PublicReadView):
    """``GET /locations/countries/{code}/types/`` — that country's tiers."""

    @extend_schema(
        responses={
            200: inline_serializer(
                name="CountryTypeListResponse",
                fields={"data": LocationTypeSerializer(many=True)},
            )
        }
    )
    def get(self, request: Request, code: str, *args: object, **kwargs: object) -> Response:
        country = get_object_or_404(Country, iso2=code.upper(), is_active=True)
        types = LocationType.objects.filter(country=country).order_by("level")
        return data_response(LocationTypeSerializer(types, many=True).data)


class LocationListView(generics.ListAPIView[Location]):
    """``GET /locations/`` — children of a parent, a search, or the roots.

    04 §4: ``?parent=<uuid>&type=STREET&search=opebi&limit=20``. The three modes
    are not interchangeable and the endpoint makes the distinction explicit:

    * ``parent`` — the next level down from that node;
    * ``search`` — a name match at any tier (FR-LOC-003);
    * neither — the country's top-level nodes, which is the only sensible
      meaning of "the list" for a tree.

    ``type`` and ``country`` narrow any of the three. An unknown ``parent`` is a
    404 rather than an empty page: a client that followed a stale link should
    learn that the place is gone, not that nobody lives there.
    """

    permission_classes = (AllowAny,)
    throttle_classes = (AnonymousReadThrottle,)
    serializer_class = LocationSerializer
    ordering: ClassVar[tuple[str, ...]] = ("name", "public_id")

    def get_queryset(self) -> QuerySet[Location]:
        queryset = Location.objects.filter(is_active=True).select_related(
            "country", "location_type", "parent"
        )
        params = self.request.query_params

        parent_value = params.get("parent")
        search = params.get("search", "").strip()

        if parent_value:
            parent = self._resolve_parent(parent_value)
            queryset = queryset.filter(parent=parent)
        elif search:
            queryset = queryset.filter(name__icontains=search)
        else:
            queryset = queryset.filter(parent__isnull=True)

        type_code = params.get("type")
        if type_code:
            queryset = queryset.filter(location_type__code=type_code.upper())

        country_code = params.get("country")
        if country_code:
            queryset = queryset.filter(country__iso2=country_code.upper())

        return queryset

    @staticmethod
    def _resolve_parent(raw: str) -> Location:
        try:
            public_id = uuid.UUID(raw)
        except ValueError as exc:
            raise drf.ValidationError(
                {"parent": "Must be a UUID, as returned by this endpoint."}
            ) from exc

        parent = Location.objects.filter(public_id=public_id, is_active=True).first()
        if parent is None:
            raise drf.NotFound("No such parent location.")
        return parent


class LocationDetailView(generics.RetrieveAPIView[Location]):
    """``GET /locations/{id}/`` — a node with a bounded window of children.

    Children are capped at ``MAX_EMBEDDED_CHILDREN``. An uncapped embed would
    make one request for a state return the state and the first page of its 774
    LGAs, with the response size growing every time an LGA is added — a
    pagination bug disguised as a convenience.
    """

    permission_classes = (AllowAny,)
    throttle_classes = (AnonymousReadThrottle,)
    serializer_class = LocationDetailSerializer
    lookup_field = "public_id"

    def get_queryset(self) -> QuerySet[Location]:
        # ``to_attr`` rather than the related manager: DRF calls ``.get()`` on
        # this queryset, and a *sliced* prefetch queryset makes that raise
        # ``TypeError: Cannot filter a query once a slice has been taken``.
        # The cap is applied when the serializer reads the prefetched list
        # (``MAX_EMBEDDED_CHILDREN``), which also bounds what is sent.
        children = (
            Location.objects.filter(is_active=True).select_related("location_type").order_by("name")
        )
        return (
            Location.objects.filter(is_active=True)
            .select_related("country", "location_type", "parent")
            .prefetch_related(Prefetch("children", queryset=children, to_attr="active_children"))
        )

    @extend_schema(
        responses={
            200: inline_serializer(
                name="LocationDetailResponse",
                fields={"data": LocationDetailSerializer()},
            )
        }
    )
    def retrieve(self, request: Request, *args: object, **kwargs: object) -> Response:
        """A single resource is wrapped like every other response body.

        ``RetrieveAPIView`` returns the bare object; the contract of 04 §1.7
        is ``{"data": ...}``. The envelope is applied here rather than by a
        renderer or a response-finalising mixin, because this project has one
        place that builds bodies and a global rewrite would also touch errors.
        """
        instance = self.get_object()
        return data_response(self.get_serializer(instance).data)
