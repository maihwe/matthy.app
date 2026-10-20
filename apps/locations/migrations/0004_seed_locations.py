"""Seed the launch data — 03 §4.5, PHASE_1_PLAN S3.

Three sets of rows, all idempotent (``update_or_create``), so applying this
migration twice — which a restored dump can cause — changes nothing:

* **Currencies**, including the awkward exponents. JPY (0), KWD (3) and XOF/XAF
  (0) are seeded from the first migration on purpose: every test and every code
  path meets them before a market depends on them, rather than discovering an
  assumption about "two decimal places" in production.
* **Nigeria**, the one active country at launch, with its currency.
* **The five Nigerian tiers** and the **37 level-1 places** (36 states and the
  Federal Capital Territory). LGAs and below are administratively loaded; 03
  seeds the states because they are a closed, stable list and because the
  group-hosting flow needs a non-empty tree to be testable.

``public_id`` values are UUIDv5 of ``NG:<slug>``. Deterministic ids mean a
second environment seeded from the same migration holds the same identifiers,
so a link from a support ticket resolves in staging as well as production.

Reverse is a no-op. Un-seeding would delete rows that groups and profiles may
now reference, and a migration that can destroy referents is worse than one
that leaves reference data behind when the schema is dropped anyway.
"""

from __future__ import annotations

import uuid

from django.db import migrations
from django.utils.text import slugify

# ISO 4217. `is_active` is false for everything but NGN: the row exists so the
# platform's arithmetic is exercised against it, not so a member can hold it.
_CURRENCIES = [
    # code, numeric, name, minor_units, symbol, is_active
    ("NGN", "566", "Nigerian Naira", 2, "₦", True),
    ("JPY", "392", "Japanese Yen", 0, "¥", False),
    ("KWD", "414", "Kuwaiti Dinar", 3, "KD", False),
    ("USD", "840", "United States Dollar", 2, "$", False),
    ("GBP", "826", "Pound Sterling", 2, "£", False),
    ("EUR", "978", "Euro", 2, "€", False),
    ("GHS", "936", "Ghanaian Cedi", 2, "₵", False),
    ("KES", "404", "Kenyan Shilling", 2, "KSh", False),
    ("ZAR", "710", "South African Rand", 2, "R", False),
    ("XOF", "952", "West African CFA Franc", 0, "CFA", False),
    ("XAF", "950", "Central African CFA Franc", 0, "FCFA", False),
]

_NIGERIA = {
    "iso3": "NGA",
    "name": "Nigeria",
    "dial_code": "+234",
    "currency_code": "NGN",
    "default_locale": "en-NG",
}

# level, code, singular, plural, is_leaf
_NIGERIAN_TIERS = [
    (1, "STATE", "State", "States", False),
    (2, "LGA", "LGA", "LGAs", False),
    (3, "CITY", "City/Town", "Cities/Towns", False),
    (4, "AREA", "Area", "Areas", False),
    (5, "STREET", "Street", "Streets", True),
]

_NIGERIAN_STATES = [
    "Abia",
    "Adamawa",
    "Akwa Ibom",
    "Anambra",
    "Bauchi",
    "Bayelsa",
    "Benue",
    "Borno",
    "Cross River",
    "Delta",
    "Ebonyi",
    "Edo",
    "Ekiti",
    "Enugu",
    "Federal Capital Territory",
    "Gombe",
    "Imo",
    "Jigawa",
    "Kaduna",
    "Kano",
    "Katsina",
    "Kebbi",
    "Kogi",
    "Kwara",
    "Lagos",
    "Nasarawa",
    "Niger",
    "Ogun",
    "Ondo",
    "Osun",
    "Oyo",
    "Plateau",
    "Rivers",
    "Sokoto",
    "Taraba",
    "Yobe",
    "Zamfara",
]

# The namespace for seeded public ids. Fixed forever: changing it would make
# every existing state's id unreachable by a fresh environment.
_SEED_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def seed(apps, schema_editor):
    Currency = apps.get_model("locations", "Currency")
    Country = apps.get_model("locations", "Country")
    LocationType = apps.get_model("locations", "LocationType")
    Location = apps.get_model("locations", "Location")

    for code, numeric, name, minor_units, symbol, is_active in _CURRENCIES:
        Currency.objects.update_or_create(
            code=code,
            defaults={
                "numeric_code": numeric,
                "name": name,
                "minor_units": minor_units,
                "symbol": symbol,
                "is_active": is_active,
            },
        )

    nigeria, _ = Country.objects.update_or_create(
        iso2="NG",
        defaults={
            "iso3": _NIGERIA["iso3"],
            "name": _NIGERIA["name"],
            "dial_code": _NIGERIA["dial_code"],
            "currency_id": _NIGERIA["currency_code"],
            "default_locale": _NIGERIA["default_locale"],
            "is_active": True,
        },
    )

    tiers: dict[str, object] = {}
    for level, code, singular, plural, is_leaf in _NIGERIAN_TIERS:
        tier, _ = LocationType.objects.update_or_create(
            country=nigeria,
            code=code,
            defaults={
                "name_singular": singular,
                "name_plural": plural,
                "level": level,
                "is_leaf": is_leaf,
            },
        )
        tiers[code] = tier

    state_type = tiers["STATE"]
    for name in _NIGERIAN_STATES:
        slug = slugify(name)
        Location.objects.update_or_create(
            parent=None,
            location_type=state_type,
            slug=slug,
            defaults={
                "country": nigeria,
                "name": name,
                "public_id": uuid.uuid5(_SEED_NAMESPACE, f"NG:{slug}"),
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("locations", "0003_location_views"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
