"""Factories for the ``locations`` module.

The seed migration already creates Nigeria, its five tiers and 37 states, so
these factories are for the *other* rows a test needs: a second country with a
different tier count (which is what proves the model is generic), an LGA under
a seed state, and so on.

Codes are derived from a counter rather than drawn at random: two tests in one
run must not collide on a unique constraint, and a collision that appears once
in a few hundred runs is the expensive kind of flake to diagnose.
"""

from __future__ import annotations

import string
import uuid

import factory
from factory.django import DjangoModelFactory

from apps.locations.models import Country, Currency, Location, LocationType


def _letters(index: int, width: int) -> str:
    """A deterministic uppercase letter code of ``width`` characters."""
    value = index
    chars = []
    for _ in range(width):
        chars.append(string.ascii_uppercase[value % 26])
        value //= 26
    return "".join(reversed(chars))


class CurrencyFactory(DjangoModelFactory[Currency]):
    class Meta:
        model = Currency
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: _letters(n, 3))
    name = factory.Sequence(lambda n: f"Test Currency {n}")
    minor_units = 2
    symbol = "T"
    is_active = True


class CountryFactory(DjangoModelFactory[Country]):
    class Meta:
        model = Country

    iso2 = factory.Sequence(lambda n: _letters(n, 2))
    iso3 = factory.Sequence(lambda n: _letters(n, 3))
    name = factory.Sequence(lambda n: f"Test Country {n}")
    dial_code = "+999"
    currency = factory.SubFactory(CurrencyFactory)
    default_locale = "en-TT"


class LocationTypeFactory(DjangoModelFactory[LocationType]):
    """A level-1 tier by default.

    A level-1 default is what makes ``LocationFactory`` a valid root without
    the caller having to arrange it. Tests that build a deeper tree state the
    level and the parent explicitly, which is also where the level rule is
    visible in the test.
    """

    class Meta:
        model = LocationType

    country = factory.SubFactory(CountryFactory)
    code = factory.Sequence(lambda n: f"TIER{n}")
    name_singular = factory.Sequence(lambda n: f"Tier {n}")
    name_plural = factory.Sequence(lambda n: f"Tiers {n}")
    level = 1
    is_leaf = False


class LocationFactory(DjangoModelFactory[Location]):
    class Meta:
        model = Location

    public_id = factory.LazyFunction(uuid.uuid4)
    # `..` is the model being built; the type and the location must agree on
    # the country or the database trigger refuses the row.
    country = factory.SubFactory(CountryFactory)
    location_type = factory.SubFactory(
        LocationTypeFactory,
        country=factory.SelfAttribute("..country"),
    )
    parent = None
    name = factory.Sequence(lambda n: f"Test Place {n}")
    slug = factory.Sequence(lambda n: f"test-place-{n}")
    is_active = True
