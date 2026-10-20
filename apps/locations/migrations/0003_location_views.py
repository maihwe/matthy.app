"""Read-only views over the location tree — 03 §4.5.1.

§27 names ``states``, ``lgas``, ``cities``, ``areas`` and ``streets`` as core
entities. The generic model stores one typed tree instead, so these views give
the named entities back for querying and reporting without a second copy of the
data: a view is a definition, not a table, and it cannot drift from the rows it
selects.

They are read-only in the application — no model or endpoint writes through
them, and every write goes to ``locations``. PostgreSQL would permit an UPDATE
through a simple view, so the protection is the absent write path, not a claim
about the database.
"""

from __future__ import annotations

from django.db import migrations

# One view per tier §27 names. The type lookup is by code because the view must
# work after a country adds a tier without a code change: the definition asks
# for "the level called STATE", not for an id chosen at seed time.
_VIEWS = {
    "states": "STATE",
    "lgas": "LGA",
    "cities": "CITY",
    "areas": "AREA",
    "streets": "STREET",
}

CREATE_VIEWS = "\n".join(
    f"""
CREATE VIEW {view} AS
SELECT *
  FROM locations
 WHERE location_type_id IN (
       SELECT id FROM location_types WHERE code = '{code}'
 );
"""
    for view, code in _VIEWS.items()
)

DROP_VIEWS = "\n".join(f"DROP VIEW IF EXISTS {view};" for view in reversed(_VIEWS))


class Migration(migrations.Migration):
    dependencies = [
        ("locations", "0002_location_tree_trigger"),
    ]

    operations = [
        migrations.RunSQL(CREATE_VIEWS, DROP_VIEWS),
    ]
