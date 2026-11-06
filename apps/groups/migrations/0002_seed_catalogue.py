"""Frozen v1 matrix; no user-specific group configuration is reset."""

from django.db import migrations

from apps.groups.migrations._seed_v1 import seed


def forwards(apps, schema_editor):
    seed(apps, schema_editor.connection.alias)


class Migration(migrations.Migration):
    dependencies = [("groups", "0001_initial")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
