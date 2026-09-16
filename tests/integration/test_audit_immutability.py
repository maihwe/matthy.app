"""``audit_logs`` immutability, against real PostgreSQL — FR-AUD-003, SEC-AUD-003.

This is the one test file in the project that cannot be satisfied by Python.
``AuditLog.save()`` and ``AuditLog.delete()`` raise, but they are not the
control: ``QuerySet.update()``, ``QuerySet.delete()`` and a cascade from another
table never call them. The trigger is the control, and a trigger can only be
tested against the database that has it — which is the whole of 13 §3's argument
for PostgreSQL, made concrete.

**The first test proves the trigger is installed.** Without it, every other test
in this file would pass on a database where the migration had silently not
applied, and a suite that passes for the wrong reason is worse than no suite
because it is believed. It is the same discipline as
``tests/security/test_static_guarantees.py``'s ratchet self-test.

Every ``IntegrityError`` assertion is wrapped in ``transaction.atomic()``: a
failed statement aborts the surrounding transaction, and without the savepoint
every later query in the same test errors with "current transaction is aborted",
which reads as a second, unrelated bug.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, connection, transaction

from apps.audit.constants import ActorType
from apps.audit.models import AuditLog
from tests.factories.audit import AuditLogFactory, UserAuditLogFactory

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

TRIGGER_NAME = "trg_audit_logs_immutable"
FUNCTION_NAME = "raise_audit_immutable"


class TestTheTriggerIsActuallyInstalled:
    """Gate on the gate: a vacuous pass is the failure mode being prevented."""

    def test_the_trigger_exists_on_the_table(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgrelid = 'audit_logs'::regclass AND NOT tgisinternal"
            )
            installed = {row[0] for row in cursor.fetchall()}
        assert TRIGGER_NAME in installed, (
            "The immutability trigger is not installed. Migration "
            "0002_audit_immutability_trigger has not run, and every other test in "
            "this file would pass without it."
        )

    def test_the_trigger_function_exists(self) -> None:
        """A trigger can exist and reference a missing function on PostgreSQL if
        the function was dropped — ``CREATE TRIGGER`` checks at creation time and
        the dependency would normally prevent it, but a hand-run ``DROP FUNCTION
        ... CASCADE`` would not leave an obvious trace."""
        with connection.cursor() as cursor:
            cursor.execute("SELECT proname FROM pg_proc WHERE proname = %s", [FUNCTION_NAME])
            assert cursor.fetchone() is not None

    def test_the_trigger_fires_before_update_and_delete_only(self) -> None:
        """Read from ``pg_trigger.tgtype``, where each event is one bit.

        Asserted from the catalogue rather than only behaviourally, so a
        migration replacing the trigger with an UPDATE-only version fails here
        even if the DELETE tests were skipped, and so that an accidental
        ``BEFORE INSERT`` is caught before it makes every audited action in the
        platform fail at once.

        The bit assignments are PostgreSQL's, from ``pg_trigger.h``:
        1 ROW, 2 BEFORE, 4 INSERT, 8 DELETE, 16 UPDATE. They are written as named
        constants because ``tgtype & 4`` in a test is unreadable and, worse,
        unverifiable by the person reading it.
        """
        row_level, before = 0b00001, 0b00010
        on_insert, on_delete, on_update = 0b00100, 0b01000, 0b10000

        with connection.cursor() as cursor:
            cursor.execute("SELECT tgtype FROM pg_trigger WHERE tgname = %s", [TRIGGER_NAME])
            row = cursor.fetchone()
        assert row is not None
        tgtype = row[0]

        assert tgtype & row_level, "trigger is not FOR EACH ROW"
        assert tgtype & before, "trigger does not fire BEFORE the statement"
        assert tgtype & on_update, "trigger does not fire on UPDATE"
        assert tgtype & on_delete, "trigger does not fire on DELETE"
        assert not tgtype & on_insert, "trigger fires on INSERT; the table would be unusable"


class TestUpdateIsRefused:
    def test_queryset_update_raises(self) -> None:
        """The path the Python guard cannot see.

        ``QuerySet.update()`` issues an UPDATE and never touches
        ``Model.save``, so this is where immutability would be lost if it were
        enforced only in Python — and FR-AUD-003 says there is no application
        path that mutates a record, not that there is no *convenient* one.
        """
        entry = AuditLogFactory()
        with pytest.raises(IntegrityError, match="append-only"), transaction.atomic():
            AuditLog.objects.filter(pk=entry.pk).update(action="user.deleted")

    def test_the_row_is_unchanged_afterwards(self) -> None:
        entry = AuditLogFactory(action="user.suspended")
        with pytest.raises(IntegrityError), transaction.atomic():
            AuditLog.objects.filter(pk=entry.pk).update(action="user.deleted")
        entry.refresh_from_db()
        assert entry.action == "user.suspended"

    def test_raw_sql_update_raises(self) -> None:
        entry = AuditLogFactory()
        with pytest.raises(IntegrityError, match="append-only"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE audit_logs SET object_repr = %s WHERE id = %s",
                    ["rewritten", entry.pk],
                )

    def test_saving_a_loaded_instance_raises_before_reaching_the_database(self) -> None:
        """The legible failure.

        The trigger would refuse this too, but as a psycopg error surfacing from
        inside whatever test happened to be running. The Python guard names the
        rule, which is what makes the ordinary mistake debuggable.
        """
        entry = AuditLogFactory()
        entry.action = "user.deleted"
        with pytest.raises(RuntimeError, match="append-only"):
            entry.save()

    def test_saving_a_loaded_instance_is_refused_even_with_update_fields(self) -> None:
        """``update_fields`` is a common way to slip past a naive guard."""
        entry = AuditLogFactory()
        with pytest.raises(RuntimeError, match="append-only"):
            entry.save(update_fields=["action"])


class TestDeleteIsRefused:
    def test_queryset_delete_raises(self) -> None:
        entry = UserAuditLogFactory()
        with pytest.raises(IntegrityError, match="append-only"), transaction.atomic():
            AuditLog.objects.filter(pk=entry.pk).delete()

    def test_the_row_survives_a_refused_queryset_delete(self) -> None:
        entry = AuditLogFactory()
        with pytest.raises(IntegrityError), transaction.atomic():
            AuditLog.objects.filter(pk=entry.pk).delete()
        assert AuditLog.objects.filter(pk=entry.pk).exists()

    def test_deleting_the_whole_table_raises(self) -> None:
        """``.all().delete()`` is the admin bulk action.

        It goes through ``QuerySet.delete``, so it reaches the trigger and not
        ``Model.delete``. The admin also refuses the action outright
        (``AuditLogAdmin.has_delete_permission``); this proves the database is
        the floor beneath that, not a second opinion.
        """
        UserAuditLogFactory()
        with pytest.raises(IntegrityError, match="append-only"), transaction.atomic():
            AuditLog.objects.all().delete()

    def test_raw_sql_delete_raises(self) -> None:
        entry = AuditLogFactory()
        with pytest.raises(IntegrityError, match="append-only"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM audit_logs WHERE id = %s", [entry.pk])


class TestTheTriggerIsNotOverBroad:
    """Immutability must not become inability to write."""

    def test_inserts_still_work(self) -> None:
        """A trigger mistakenly declared ``BEFORE INSERT OR UPDATE OR DELETE``
        would make the table unusable, and the failure would appear as every
        audited action in the platform failing at once."""
        first = AuditLogFactory()
        second = AuditLogFactory()
        assert AuditLog.objects.filter(pk__in=[first.pk, second.pk]).count() == 2

    def test_a_user_actor_row_is_accepted(self) -> None:
        """``actor_user_id`` is a plain integer, so there is no FK for the
        database to check and the id need not exist.

        That is the accepted cost of a record outliving the user it names, and it
        is asserted here — with a deliberately non-existent id — so the
        trade-off is visible rather than discovered by whoever first tries to
        resolve an actor and finds nothing to join to.
        """
        entry = UserAuditLogFactory(actor_user_id=999_999)
        entry.refresh_from_db()
        assert entry.actor_type == ActorType.USER
        assert entry.actor_user_id == 999_999
