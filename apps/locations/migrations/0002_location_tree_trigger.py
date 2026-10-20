"""The location-tree trigger — 03 §4.5.

``CHECK`` constraints cannot join, and the rule this enforces needs two joins:
a location's parent must be exactly one level above it, and both must belong to
the same country as the type. Those facts live on ``location_types``, so this
is a trigger rather than a constraint. It is the **second** hand-written
migration in the project, after the audit immutability trigger; P1-12 named
only one because ``locations`` did not exist when it was written, and its
wording is updated in the same change.

The trigger fires on the columns that can change the shape — ``parent_id``,
``location_type_id`` and ``country_id`` — rather than on every UPDATE. A rename
does not need to pay for three joins.

Reproduce the empty migration with:

    python manage.py makemigrations locations --empty -n location_tree_trigger
"""

from __future__ import annotations

from django.db import migrations

FUNCTION_NAME = "check_location_tree"
TRIGGER_NAME = "trg_locations_check_tree"

CREATE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {FUNCTION_NAME}() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    child_level smallint;
    child_country bigint;
    parent_level smallint;
    parent_country bigint;
BEGIN
    SELECT lt.level, lt.country_id
      INTO child_level, child_country
      FROM location_types lt
     WHERE lt.id = NEW.location_type_id;

    IF child_level IS NULL THEN
        RAISE EXCEPTION
            'locations row % references unknown location_type %',
            NEW.id, NEW.location_type_id
            USING ERRCODE = 'foreign_key_violation',
                  CONSTRAINT = 'locations_type_exists';
    END IF;

    IF child_country <> NEW.country_id THEN
        RAISE EXCEPTION
            'location % and its type % belong to different countries',
            NEW.id, NEW.location_type_id
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'locations_type_country_matches';
    END IF;

    IF NEW.parent_id IS NULL THEN
        IF child_level <> 1 THEN
            RAISE EXCEPTION
                'location % is level % and must have a parent', NEW.id, child_level
                USING ERRCODE = 'check_violation',
                      CONSTRAINT = 'locations_root_is_level_one';
        END IF;
        RETURN NEW;
    END IF;

    SELECT lt.level, l.country_id
      INTO parent_level, parent_country
      FROM locations l
      JOIN location_types lt ON lt.id = l.location_type_id
     WHERE l.id = NEW.parent_id;

    IF parent_level IS NULL THEN
        RAISE EXCEPTION
            'location % references unknown parent %', NEW.id, NEW.parent_id
            USING ERRCODE = 'foreign_key_violation',
                  CONSTRAINT = 'locations_parent_exists';
    END IF;

    IF parent_country <> NEW.country_id THEN
        RAISE EXCEPTION
            'location % and its parent % belong to different countries',
            NEW.id, NEW.parent_id
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'locations_parent_country_matches';
    END IF;

    IF parent_level + 1 <> child_level THEN
        RAISE EXCEPTION
            'location % is level % but its parent % is level %; a child must be exactly one below',
            NEW.id, child_level, NEW.parent_id, parent_level
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'locations_parent_is_one_level_above';
    END IF;

    RETURN NEW;
END;
$$;
"""

DROP_FUNCTION = f"DROP FUNCTION IF EXISTS {FUNCTION_NAME}()"

CREATE_TRIGGER = f"""
CREATE TRIGGER {TRIGGER_NAME}
BEFORE INSERT OR UPDATE OF parent_id, location_type_id, country_id
ON locations
FOR EACH ROW EXECUTE FUNCTION {FUNCTION_NAME}();
"""

DROP_TRIGGER = f"DROP TRIGGER IF EXISTS {TRIGGER_NAME} ON locations"


class Migration(migrations.Migration):
    dependencies = [
        ("locations", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(CREATE_FUNCTION, DROP_FUNCTION),
        migrations.RunSQL(CREATE_TRIGGER, DROP_TRIGGER),
    ]
