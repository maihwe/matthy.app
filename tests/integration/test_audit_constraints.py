"""``audit_logs`` constraints, against real PostgreSQL.

The three CHECK constraints and the NOT NULLs are the difference between an
audit table that can be searched and one that merely exists. ``audit_logs`` is
written by every module in the platform, through one function — and a function
is a Python-level guarantee that a determined caller, a data migration, or a
future ``bulk_create`` can step around. The database is the floor.

These are asserted by inserting bad rows rather than by reading the ``Q`` objects
back out of ``_meta``. Reading them would assert that each constraint is
*spelled* a particular way; inserting asserts that it *works*, and only the
second survives a change in how Django renders a ``Q``.

Every assertion is wrapped in ``transaction.atomic()``: a failed statement
aborts the surrounding transaction, and without the savepoint every later query
in the same test errors with "current transaction is aborted".
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, connection, transaction

from apps.audit.constants import ActorType
from apps.audit.models import AuditLog
from apps.audit.services import record
from tests.factories.audit import audit_values

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

# A raw insert, deliberately: `Model.save` and `record()` both validate before
# reaching the database, so neither can show whether the database would have
# refused the row on its own. `object_repr` and `occurred_at` are supplied
# because their defaults live in Django, not in the column definition — a raw
# insert has to provide them.
_INSERT = """
INSERT INTO audit_logs (actor_type, action, object_type, object_id, object_repr, occurred_at)
VALUES (%s, %s, %s, %s, '', now())
"""


def _insert(actor_type: object, action: object, object_type: object) -> None:
    """Write a row with no Python-side validation in the way."""
    with connection.cursor() as cursor:
        cursor.execute(_INSERT, [actor_type, action, object_type, "1"])


class TestTheConstraintsAreActuallyInstalled:
    """Gate on the gate.

    A CHECK that was never created enforces nothing, and every refusal below
    would still pass on a table whose column list had a typo in it.
    """

    def test_a_valid_row_is_accepted(self) -> None:
        with transaction.atomic():
            _insert(ActorType.SYSTEM, "user.suspended", "user")
        assert AuditLog.objects.count() == 1


class TestActorType:
    """``choices=`` is enforced by forms and serializers, not by the ORM."""

    def test_an_unknown_actor_type_is_refused(self) -> None:
        refused = pytest.raises(IntegrityError, match="audit_logs_actor_type_valid")
        with refused, transaction.atomic():
            _insert("SUPERUSER", "user.suspended", "user")

    def test_an_empty_actor_type_is_refused(self) -> None:
        """The failure this prevents is a row that cannot answer "was this a
        human?", which is the first question anyone asks of an audit trail."""
        refused = pytest.raises(IntegrityError, match="audit_logs_actor_type_valid")
        with refused, transaction.atomic():
            _insert("", "user.suspended", "user")

    def test_a_null_actor_type_is_refused(self) -> None:
        refused = pytest.raises(IntegrityError, match="null value in column")
        with refused, transaction.atomic():
            _insert(None, "user.suspended", "user")

    def test_the_orm_cannot_write_one_either(self) -> None:
        """The backstop matters because ``record()`` takes a plain string at
        runtime.

        ``actor_type: ActorType`` is a type hint, and Python enforces nothing. A
        caller that passes ``"SUPERUSER"`` gets past every check in ``record()``:
        it is not ``SYSTEM``, and the actor rule only requires that ``USER`` and
        ``ADMIN`` entries name a user — which this one does. So the vocabulary is
        closed here, in the column, or it is not closed at all.
        """
        refused = pytest.raises(IntegrityError, match="audit_logs_actor_type_valid")
        with refused, transaction.atomic():
            record(**audit_values(actor_type="SUPERUSER", actor_user_id=1))


class TestActionAndObjectType:
    def test_an_empty_action_is_refused(self) -> None:
        """An unnamed action cannot be searched for, and the action index is the
        only way to answer "who has suspended anyone this month?" (SEC-AUD-005)."""
        refused = pytest.raises(IntegrityError, match="audit_logs_action_not_empty")
        with refused, transaction.atomic():
            _insert(ActorType.SYSTEM, "", "user")

    def test_an_empty_object_type_is_refused(self) -> None:
        """A blank object_type leaves a row the investigation index cannot
        reach — present in the table and invisible to the query that reads it."""
        refused = pytest.raises(IntegrityError, match="audit_logs_object_type_not_empty")
        with refused, transaction.atomic():
            _insert(ActorType.SYSTEM, "user.suspended", "")
