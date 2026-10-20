"""The ``locations`` module — 03 §4.5, FR-LOC-001…006.

Layer: **L0**. It imports nothing from other project modules, so `accounts`
can reference a location without creating a dependency on the group hierarchy.

The module exists because the administrative vocabulary is data, not schema
(FR-LOC-001). One typed tree — ``locations`` — holds every tier; the levels
(State, LGA, City/Town, Area, Street) are rows in ``location_types``. Adding a
country with six tiers is therefore a seed, not a migration. §27's named
entities (``states``, ``lgas``, …) survive as read-only views over the tree
(§4.5.1), which is how the requirement and the generic model are both
satisfied without a second copy of the data.
"""

from __future__ import annotations

from django.apps import AppConfig


class LocationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.locations"
    label = "locations"
    verbose_name = "Locations"
