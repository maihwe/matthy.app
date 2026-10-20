"""Database guarantees for ``locations`` — 03 §4.5, 13 §3.

Constraints are tested at the database, not through ``full_clean()``. A model
validator is advice; a ``CHECK``, a unique index with NULLS NOT DISTINCT, and
the tree trigger are controls. The distinction matters because seed scripts,
admin bulk actions and future services all write through the ORM's ``save()``,
which does not call ``clean()``.

The seed migration is also asserted here. It is data with a contract: 37
level-1 places and five tiers for Nigeria, plus the currency exponents the
financial modules will rely on.
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.locations.models import (
    Country,
    Currency,
    Lga,
    Location,
    LocationType,
    State,
)
from tests.factories.locations import (
    CountryFactory,
    LocationFactory,
    LocationTypeFactory,
)

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


def _nigeria() -> Country:
    return Country.objects.get(iso2="NG")


def _tier(code: str) -> LocationType:
    return LocationType.objects.get(country=_nigeria(), code=code)


class TestSeedData:
    """The launch seed is part of the deliverable, not a fixture of convenience."""

    def test_the_awkward_currency_exponents_are_present(self) -> None:
        assert Currency.objects.get(code="JPY").minor_units == 0
        assert Currency.objects.get(code="KWD").minor_units == 3
        assert Currency.objects.get(code="XOF").minor_units == 0
        assert Currency.objects.get(code="NGN").minor_units == 2

    def test_only_the_launch_currency_is_active(self) -> None:
        assert list(Currency.objects.filter(is_active=True).values_list("code", flat=True)) == [
            "NGN"
        ]

    def test_nigeria_uses_the_naira(self) -> None:
        nigeria = _nigeria()
        assert nigeria.currency_id == "NGN"
        assert nigeria.dial_code == "+234"

    def test_the_five_nigerian_tiers_are_seeded_in_order(self) -> None:
        assert list(
            LocationType.objects.filter(country=_nigeria()).values_list("code", "level", "is_leaf")
        ) == [
            ("STATE", 1, False),
            ("LGA", 2, False),
            ("CITY", 3, False),
            ("AREA", 4, False),
            ("STREET", 5, True),
        ]

    def test_the_thirty_seven_level_one_places_are_seeded(self) -> None:
        states = Location.objects.filter(country=_nigeria(), parent__isnull=True)
        assert states.count() == 37
        assert states.filter(name="Federal Capital Territory").exists()

    def test_the_named_views_read_the_generic_tree(self) -> None:
        assert State.objects.count() == 37
        assert Lga.objects.count() == 0
        assert State.objects.filter(name="Lagos").exists()

    def test_seed_identifiers_are_deterministic(self) -> None:
        """Environment parity: a support link resolves in staging too.

        UUIDv5 over ``NG:<slug>`` with a fixed namespace is the contract. A
        random id would make every environment's Lagos a different identifier,
        and a link from a support ticket would 404 wherever it was opened.
        """
        namespace = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")
        lagos = Location.objects.get(slug="lagos")
        assert lagos.public_id == uuid.uuid5(namespace, "NG:lagos")


class TestUniqueKeys:
    def test_two_root_states_may_not_share_a_slug(self) -> None:
        """NULLS NOT DISTINCT (P1-3): SQL would otherwise let both NULL parents
        escape the unique key."""
        nigeria = _nigeria()
        with pytest.raises(IntegrityError), transaction.atomic():
            Location.objects.create(
                country=nigeria,
                location_type=_tier("STATE"),
                parent=None,
                name="Lagos",
                slug="lagos",
            )

    def test_two_siblings_may_not_share_a_slug(self) -> None:
        parent = Location.objects.get(slug="lagos")
        lga_type = _tier("LGA")
        Location.objects.create(
            country=parent.country,
            location_type=lga_type,
            parent=parent,
            name="Ikeja",
            slug="ikeja",
        )
        # A different type below the same parent is allowed by the unique key,
        # but the trigger refuses the level; so this test asserts the unique
        # key with the same type, which is the case it governs.
        with pytest.raises(IntegrityError), transaction.atomic():
            Location.objects.create(
                country=parent.country,
                location_type=lga_type,
                parent=parent,
                name="Ikeja II",
                slug="ikeja",
            )

    def test_country_codes_are_unique(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            Country.objects.create(
                iso2="NG",
                iso3="XXX",
                name="Duplicate",
                dial_code="+999",
                currency_id="NGN",
                default_locale="en-NG",
            )

    def test_a_country_with_the_same_name_is_fine(self) -> None:
        """Names are not identifiers; only the ISO codes are."""
        CountryFactory(name="Nigeria")


class TestCheckConstraints:
    def test_a_lowercase_currency_code_is_refused(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            Currency.objects.create(code="ngn", name="n", minor_units=2, symbol="n")

    def test_an_absurd_exponent_is_refused(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            Currency.objects.create(code="ZZZ", name="z", minor_units=5, symbol="z")

    def test_a_zero_cash_increment_is_refused(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            Currency.objects.create(
                code="ZZY",
                name="z",
                minor_units=2,
                symbol="z",
                cash_rounding_increment=0,
            )

    def test_latitude_outside_the_planet_is_refused(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            LocationFactory(latitude="91.000000")

    def test_longitude_outside_the_planet_is_refused(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            LocationFactory(longitude="-180.000001")

    def test_a_slug_that_is_not_a_slug_is_refused(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            LocationFactory(slug="Not A Slug")

    def test_an_lga_code_outside_the_vocabulary_is_refused(self) -> None:
        country = CountryFactory()
        with pytest.raises(IntegrityError), transaction.atomic():
            LocationType.objects.create(
                country=country,
                code="state",
                name_singular="s",
                name_plural="p",
                level=1,
            )


class TestTreeTrigger:
    """03 §4.5: the level rule needs a join, so it is a trigger."""

    def test_a_child_must_be_exactly_one_level_below_its_parent(self) -> None:
        parent = Location.objects.get(slug="lagos")
        with pytest.raises(IntegrityError), transaction.atomic():
            Location.objects.create(
                country=parent.country,
                location_type=_tier("CITY"),
                parent=parent,
                name="Ikeja City",
                slug="ikeja-city",
            )

    def test_a_root_must_be_level_one(self) -> None:
        nigeria = _nigeria()
        with pytest.raises(IntegrityError), transaction.atomic():
            Location.objects.create(
                country=nigeria,
                location_type=_tier("LGA"),
                parent=None,
                name="Nowhere LGA",
                slug="nowhere-lga",
            )

    def test_a_child_and_its_parent_must_share_a_country(self) -> None:
        parent = Location.objects.get(slug="lagos")
        other = CountryFactory()
        other_lga = LocationTypeFactory(country=other, code="LGA", level=2)
        with pytest.raises(IntegrityError), transaction.atomic():
            Location.objects.create(
                country=other,
                location_type=other_lga,
                parent=parent,
                name="Foreign LGA",
                slug="foreign-lga",
            )

    def test_a_location_type_must_belong_to_its_country(self) -> None:
        nigeria = _nigeria()
        other = CountryFactory()
        foreign_type = LocationTypeFactory(country=other, code="STATE", level=1)
        with pytest.raises(IntegrityError), transaction.atomic():
            Location.objects.create(
                country=nigeria,
                location_type=foreign_type,
                parent=None,
                name="Mismatch",
                slug="mismatch",
            )

    def test_a_valid_child_is_accepted(self) -> None:
        parent = Location.objects.get(slug="lagos")
        lga = Location.objects.create(
            country=parent.country,
            location_type=_tier("LGA"),
            parent=parent,
            name="Ikeja",
            slug="ikeja",
        )
        assert lga.level == 2
        assert lga in parent.children.all()

    def test_moving_a_node_under_an_incompatible_parent_is_refused(self) -> None:
        """The trigger fires on UPDATE of parent_id, not only INSERT.

        Moving an LGA under a *state* is refused, but for the level rule the
        clearer case is an LGA moved under another LGA: same country, same
        type, and still not one level below — which proves the rule is about
        depth and not about the country or the type.
        """
        lagos = Location.objects.get(slug="lagos")
        kano = Location.objects.get(slug="kano")
        lga_type = _tier("LGA")
        lga = Location.objects.create(
            country=lagos.country,
            location_type=lga_type,
            parent=lagos,
            name="Ikeja",
            slug="ikeja-move",
        )
        other_lga = Location.objects.create(
            country=kano.country,
            location_type=lga_type,
            parent=kano,
            name="Nassarawa",
            slug="nassarawa-move",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            Location.objects.filter(pk=lga.pk).update(parent=other_lga)

    def test_the_model_form_error_is_legible(self) -> None:
        """``clean()`` is the readable layer in front of the trigger."""
        parent = Location.objects.get(slug="lagos")
        location = Location(
            country=parent.country,
            location_type=_tier("CITY"),
            parent=parent,
            name="Ikeja City",
            slug="ikeja-city",
        )
        with pytest.raises(ValidationError) as caught:
            location.full_clean()
        assert "parent" in caught.value.message_dict


class TestSearchIndexDeclaration:
    def test_the_trigram_index_is_part_of_the_model_state(self) -> None:
        """Production and CI create it; a contrib-less local install skips the
        physical object with a warning (see migration 0001). The declaration is
        what `makemigrations --check` protects, so it is asserted here."""
        indexes = Location._meta.indexes
        trigram = [index for index in indexes if isinstance(index, GinIndex)]
        assert len(trigram) == 1
        assert trigram[0].name == "locations_name_trgm_idx"
