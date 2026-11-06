"""Cross-table invariants and immediate permission/session invalidation."""

from pathlib import Path

from django.db import migrations

SQL = (Path(__file__).parent / "guards_v1.sql").read_text()
REVERSE = """
DROP FUNCTION groups_guard_catalogue() CASCADE;
DROP FUNCTION groups_guard_identity() CASCADE;
DROP FUNCTION groups_keep_super_admin() CASCADE;
DROP FUNCTION groups_keep_owner() CASCADE;
DROP FUNCTION groups_guard_platform_assignment() CASCADE;
DROP FUNCTION groups_guard_assignment() CASCADE;
DROP FUNCTION groups_guard_grant() CASCADE;
DROP FUNCTION groups_guard_role() CASCADE;
DROP FUNCTION groups_keep_history() CASCADE;
DROP FUNCTION groups_membership_history() CASCADE;
DROP FUNCTION groups_invalidate() CASCADE;
DROP FUNCTION groups_touch_user(bigint) CASCADE;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("groups", "0004_membership_actor"),
        ("accounts", "0006_user_authorization_version_user_role_mfa_required_and_more"),
    ]
    operations = [migrations.RunSQL(SQL, REVERSE)]
