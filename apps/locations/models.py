"""The ``locations`` data model — 03 §4.5, FR-LOC-001…006.

Four tables, and one decision running through all of them: **a place is a node
in a typed tree, not a column on another table.**

``currencies`` is separate from ``countries`` because a currency is shared and
has one definition (03 §4.5.2). Two countries using XOF must not be able to
disagree about its exponent, and a foreign key makes that disagreement
unrepresentable rather than merely discouraged.

``location_types`` is what makes FR-LOC-001 true. The levels are rows, so a
country that administers an extra tier seeds six rows instead of requiring a
schema change. ``locations`` is the tree itself; its shape is enforced twice —
by constraints where SQL can express the rule, and by a trigger where the rule
needs a join to ``location_types`` (migration 0002).

The read-only view models at the foot of the file give §27's names (``states``,
``lgas``, …) to the generic tree. They are unmanaged: Django never creates or
migrates them, and all writes go through ``locations``, so there is no
dual-write and no drift.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from apps.locations.constants import RoundingMode

__all__ = [
    "Area",
    "City",
    "Country",
    "Currency",
    "Lga",
    "Location",
    "LocationType",
    "State",
    "Street",
]

CURRENCY_CODE_LENGTH = 3
ISO2_LENGTH = 2
ISO3_LENGTH = 3
LOCATION_NAME_MAX_LENGTH = 160
LOCATION_SLUG_MAX_LENGTH = 160
LOCATION_CODE_MAX_LENGTH = 32
LOCATION_TYPE_CODE_MAX_LENGTH = 16
DIAL_CODE_MAX_LENGTH = 8
LOCALE_MAX_LENGTH = 16

# 03 §4.5 does not state precision. Six decimal places is ~11 cm at the
# equator — an address does not need better, and nine digits before the point
# holds every coordinate on Earth.
COORDINATE_MAX_DIGITS = 9
COORDINATE_DECIMAL_PLACES = 6


class Currency(models.Model):
    """``currencies`` — one definition per ISO 4217 code (03 §4.5.2).

    The code is the primary key, not a surrogate: a currency has exactly one
    natural identifier, and an integer key would let two rows claim ``NGN``
    unless a separate unique constraint existed anyway. The awkward exponents
    are seeded from the first migration — 0 for JPY/XOF, 3 for KWD — so every
    code path meets them before a market depends on them.
    """

    code = models.CharField(max_length=CURRENCY_CODE_LENGTH, primary_key=True)
    numeric_code = models.CharField(
        max_length=3,
        null=True,
        blank=True,
        default=None,
        help_text="ISO 4217 numeric form, e.g. '566' for NGN. Used by some settlement formats.",
    )
    name = models.CharField(max_length=100)
    minor_units = models.SmallIntegerField(
        help_text="Decimal exponent: 2 for NGN, 0 for JPY, 3 for KWD.",
    )
    rounding_mode = models.CharField(
        max_length=16,
        choices=RoundingMode.choices,
        default=RoundingMode.HALF_UP,
        help_text="06 §3.4.1. Data, not a constant, so a jurisdiction can be served without a deploy.",
    )
    cash_rounding_increment = models.BigIntegerField(
        null=True,
        blank=True,
        default=None,
        help_text=(
            "Minor units to round physical-cash transactions to, or NULL for none. "
            "e.g. 5 where the smallest coin has been withdrawn."
        ),
    )
    symbol = models.CharField(max_length=8)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "currencies"
        verbose_name = _("currency")
        verbose_name_plural = _("currencies")
        ordering = ("code",)
        constraints = [
            models.CheckConstraint(
                condition=Q(code__regex=r"^[A-Z]{3}$"),
                name="currencies_code_iso_4217",
            ),
            models.CheckConstraint(
                condition=Q(minor_units__gte=0) & Q(minor_units__lte=4),
                name="currencies_minor_units_range",
            ),
            models.CheckConstraint(
                condition=Q(cash_rounding_increment__isnull=True)
                | Q(cash_rounding_increment__gt=0),
                name="currencies_cash_increment_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} ({self.name})"


class Country(models.Model):
    """``countries`` — 03 §4.5. A country's identity and its dialling plan.

    No administrative tiers are named here. Which tiers exist, and how many, is
    rows in ``location_types`` keyed by this row.
    """

    iso2 = models.CharField(max_length=ISO2_LENGTH, unique=True)
    iso3 = models.CharField(max_length=ISO3_LENGTH, unique=True)
    name = models.CharField(max_length=100)
    dial_code = models.CharField(
        max_length=DIAL_CODE_MAX_LENGTH,
        help_text="E.164 dialling prefix including '+', e.g. '+234'.",
    )
    currency = models.ForeignKey(
        Currency,
        on_delete=models.PROTECT,
        related_name="countries",
        # PROTECT, not CASCADE: deleting a currency that a country uses would
        # silently change what money means for that country. It must be an
        # administrative act.
    )
    default_locale = models.CharField(max_length=LOCALE_MAX_LENGTH, default="en-NG")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "countries"
        verbose_name = _("country")
        verbose_name_plural = _("countries")
        ordering = ("name",)
        constraints = [
            models.CheckConstraint(
                condition=Q(iso2__regex=r"^[A-Z]{2}$"),
                name="countries_iso2_uppercase",
            ),
            models.CheckConstraint(
                condition=Q(iso3__regex=r"^[A-Z]{3}$"),
                name="countries_iso3_uppercase",
            ),
            models.CheckConstraint(
                condition=Q(dial_code__startswith="+") & Q(dial_code__regex=r"^\+[0-9]{1,4}$"),
                name="countries_dial_code_e164",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.iso2} ({self.name})"


class LocationType(models.Model):
    """``location_types`` — the tiers a country defines (03 §4.5).

    ``level`` is 1-based and contiguous within a country, and the tree trigger
    refuses a child whose tier is not exactly its parent's level plus one. The
    uniqueness of ``(country, level)`` is what makes that rule meaningful: with
    two tiers at one level, "exactly one below" has two answers.
    """

    country = models.ForeignKey(Country, on_delete=models.PROTECT, related_name="location_types")
    code = models.CharField(
        max_length=LOCATION_TYPE_CODE_MAX_LENGTH,
        help_text="Stable identifier, e.g. STATE, LGA, STREET. Uppercase.",
    )
    name_singular = models.CharField(max_length=50)
    name_plural = models.CharField(max_length=50)
    level = models.SmallIntegerField(help_text="1-based; STATE is 1 in Nigeria.")
    is_leaf = models.BooleanField(
        default=False,
        help_text="True when the country defines nothing below this tier (STREET in Nigeria).",
    )

    class Meta:
        db_table = "location_types"
        verbose_name = _("location type")
        verbose_name_plural = _("location types")
        ordering = ("country", "level")
        constraints = [
            models.UniqueConstraint(
                fields=["country", "code"],
                name="location_types_country_code_uniq",
            ),
            models.UniqueConstraint(
                fields=["country", "level"],
                name="location_types_country_level_uniq",
            ),
            models.CheckConstraint(
                condition=Q(level__gte=1),
                name="location_types_level_positive",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=r"^[A-Z][A-Z0-9_]*$"),
                name="location_types_code_uppercase",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.country.iso2}:{self.code}"


class Location(models.Model):
    """``locations`` — one node in a country's administrative tree.

    ``country`` is stored on every row rather than reached by walking to the
    root. That is deliberate redundancy: it makes ``(country, is_active)``
    indexable for the browse query, and the tree trigger asserts it matches the
    parent, so the two cannot disagree.

    The unique key is ``(parent, location_type, slug)`` with NULLS NOT
    DISTINCT (P1-3), so two root states cannot share a slug — the case a plain
    unique constraint would let through, because SQL treats NULLs as distinct.
    """

    public_id = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        help_text="External identifier. Safe to expose; the integer id is not.",
    )
    country = models.ForeignKey(Country, on_delete=models.PROTECT, related_name="locations")
    location_type = models.ForeignKey(
        LocationType,
        on_delete=models.PROTECT,
        related_name="locations",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        default=None,
        related_name="children",
        # PROTECT: deleting a state must not silently cascade a tree of LGAs,
        # streets and the groups seated on them out of existence.
    )
    name = models.CharField(max_length=LOCATION_NAME_MAX_LENGTH)
    slug = models.SlugField(max_length=LOCATION_SLUG_MAX_LENGTH)
    code = models.CharField(
        max_length=LOCATION_CODE_MAX_LENGTH,
        null=True,
        blank=True,
        default=None,
        help_text="Official code where the country publishes one, e.g. a state code.",
    )
    latitude = models.DecimalField(
        max_digits=COORDINATE_MAX_DIGITS,
        decimal_places=COORDINATE_DECIMAL_PLACES,
        null=True,
        blank=True,
        default=None,
    )
    longitude = models.DecimalField(
        max_digits=COORDINATE_MAX_DIGITS,
        decimal_places=COORDINATE_DECIMAL_PLACES,
        null=True,
        blank=True,
        default=None,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "locations"
        verbose_name = _("location")
        verbose_name_plural = _("locations")
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(
                fields=["parent", "location_type", "slug"],
                name="locations_parent_type_slug_uniq",
                nulls_distinct=False,
            ),
            models.CheckConstraint(
                condition=Q(slug__regex=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
                name="locations_slug_format",
            ),
            models.CheckConstraint(
                condition=Q(latitude__isnull=True) | Q(latitude__gte=-90) & Q(latitude__lte=90),
                name="locations_latitude_range",
            ),
            models.CheckConstraint(
                condition=Q(longitude__isnull=True)
                | Q(longitude__gte=-180) & Q(longitude__lte=180),
                name="locations_longitude_range",
            ),
            models.CheckConstraint(
                condition=Q(parent__isnull=True) | ~Q(id=F("parent_id")),
                name="locations_parent_not_self",
            ),
        ]
        indexes = [
            # FR-LOC-003: "list the children of X, ordered by name" is the
            # browse query; the index carries the ordering too.
            models.Index(
                fields=["location_type", "parent", "name"],
                name="locations_type_parent_name_idx",
            ),
            # "what is directly under this node?" without the type.
            models.Index(fields=["parent"], name="locations_parent_idx"),
            # "active places in Nigeria" — the country-scoped browse and the
            # endpoint's default filter.
            models.Index(fields=["country", "is_active"], name="locations_country_active_idx"),
            # FR-LOC-003's search: a trigram index makes ILIKE '%opebi%' use an
            # index instead of scanning the table. Requires pg_trgm, created in
            # migration 0001.
            GinIndex(
                fields=["name"],
                name="locations_name_trgm_idx",
                opclasses=["gin_trgm_ops"],
            ),
        ]

    objects: ClassVar[models.Manager[Location]] = models.Manager()

    def __str__(self) -> str:
        return f"Location({self.slug}, {self.country.iso2})"

    @property
    def level(self) -> int:
        return self.location_type.level

    @property
    def is_leaf(self) -> bool:
        return self.location_type.is_leaf

    def clean(self) -> None:
        """Validate the tree shape in Python, for admin and serializer paths.

        The database trigger (migration 0002) is the control that cannot be
        bypassed; this is the legible error in front of it. ``save()`` does not
        call ``clean()`` — Django does not do that for any model — so a caller
        that means to be safe calls ``full_clean()``, and a caller that does not
        still cannot write an inconsistent tree.
        """
        super().clean()
        # An unsaved instance has no foreign-key id yet even though django-stubs
        # types the `_id` attribute as a plain int. The falsy test is what an
        # unsaved model actually hits; `full_clean()` before a save must not
        # raise on "the type is missing" when the caller is about to supply it.
        if not self.location_type_id or not self.country_id:
            return

        type_country_id = self.location_type.country_id
        if type_country_id != self.country_id:
            raise ValidationError(
                {"location_type": "A location's type must belong to its country."}
            )

        if self.parent_id is None:
            if self.location_type.level != 1:
                raise ValidationError({"parent": "Only a level-1 location may have no parent."})
            return

        parent = self.parent
        if parent is None:
            raise ValidationError({"parent": "The parent location does not exist."})
        if parent.country_id != self.country_id:
            raise ValidationError({"parent": "A location's parent must be in its country."})
        if parent.location_type.level + 1 != self.location_type.level:
            raise ValidationError(
                {"parent": "A location's parent must be exactly one level above it."}
            )


class _LocationViewBase(models.Model):
    """The shape of a read-only view over ``locations`` (03 §4.5.1).

    ``managed = False``: Django neither creates nor migrates these tables; the
    views are created by migration 0003. They are read-only because no write
    path is exposed — the admin and the API address ``Location`` — not because
    PostgreSQL would refuse an UPDATE through a simple view.
    """

    id = models.BigIntegerField(primary_key=True)
    public_id = models.UUIDField()
    country = models.ForeignKey(
        Country,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="+",
    )
    location_type = models.ForeignKey(
        LocationType,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="+",
    )
    parent = models.ForeignKey(
        Location,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        null=True,
        related_name="+",
    )
    name = models.CharField(max_length=LOCATION_NAME_MAX_LENGTH)
    slug = models.SlugField(max_length=LOCATION_SLUG_MAX_LENGTH)
    code = models.CharField(max_length=LOCATION_CODE_MAX_LENGTH, null=True)
    latitude = models.DecimalField(
        max_digits=COORDINATE_MAX_DIGITS,
        decimal_places=COORDINATE_DECIMAL_PLACES,
        null=True,
    )
    longitude = models.DecimalField(
        max_digits=COORDINATE_MAX_DIGITS,
        decimal_places=COORDINATE_DECIMAL_PLACES,
        null=True,
    )
    is_active = models.BooleanField()
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        abstract = True
        managed = False

    def __str__(self) -> str:
        return f"{type(self).__name__}({self.slug})"


class State(_LocationViewBase):
    """§27's ``states`` — the level-1 view over ``locations``."""

    class Meta(_LocationViewBase.Meta):
        db_table = "states"
        verbose_name = _("state")
        verbose_name_plural = _("states")


class Lga(_LocationViewBase):
    """§27's ``lgas`` — the level-2 view over ``locations``."""

    class Meta(_LocationViewBase.Meta):
        db_table = "lgas"
        verbose_name = _("LGA")
        verbose_name_plural = _("LGAs")


class City(_LocationViewBase):
    """§27's ``cities`` — the level-3 view over ``locations``."""

    class Meta(_LocationViewBase.Meta):
        db_table = "cities"
        verbose_name = _("city")
        verbose_name_plural = _("cities")


class Area(_LocationViewBase):
    """§27's ``areas`` — the level-4 view over ``locations``."""

    class Meta(_LocationViewBase.Meta):
        db_table = "areas"
        verbose_name = _("area")
        verbose_name_plural = _("areas")


class Street(_LocationViewBase):
    """§27's ``streets`` — the level-5 view over ``locations``."""

    class Meta(_LocationViewBase.Meta):
        db_table = "streets"
        verbose_name = _("street")
        verbose_name_plural = _("streets")
