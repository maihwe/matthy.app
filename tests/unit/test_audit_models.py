"""``audit`` model structure — the decisions, asserted rather than commented.

The centrepiece is that ``AuditLog`` has **no relations at all**. That is the
one structural property the whole module rests on: a cascade must never be able
to delete an audit row, a record must outlive what it describes, and ``audit``
must sit at L0 so that every other module can import it without a cycle. All
three follow from the absence asserted here, and all three would be quietly
undone by one helpful-looking ``ForeignKey``.

No database is needed: ``_meta`` is populated at import.
"""

from __future__ import annotations

import pytest
from django.db.models import (
    BigAutoField,
    BigIntegerField,
    CharField,
    DateTimeField,
    ForeignKey,
    GenericIPAddressField,
    JSONField,
    ManyToManyField,
    OneToOneField,
)

from apps.audit.constants import ActorType
from apps.audit.models import ACTION_MAX_LENGTH, AuditLog

pytestmark = pytest.mark.unit


class TestHasNoRelations:
    """The L0 contract, enforced structurally (02 §4.3)."""

    def test_no_field_on_the_model_is_a_relation(self) -> None:
        """``actor_user_id``, ``device_id`` and ``session_id`` are integers.

        03 §12.1 names them as ``_id`` columns, which is how Django names a
        ForeignKey's column, so the tempting reading is that they are FKs. They
        are not, and this test is what stops a later "fix" from making them so:
        an FK to ``accounts.User`` with the default ``on_delete=CASCADE`` would
        delete the record of what a user did at the moment the user was removed,
        which is precisely the record an investigation needs.
        """
        relations = [
            field
            for field in AuditLog._meta.get_fields()
            if isinstance(field, (ForeignKey, ManyToManyField, OneToOneField))
        ]
        assert relations == []

    def test_the_device_and_session_columns_are_plain_integers(self) -> None:
        for name in ("actor_user_id", "device_id", "session_id"):
            field = AuditLog._meta.get_field(name)
            assert isinstance(field, BigIntegerField), name
            assert field.null is True, name

    def test_the_reverse_relations_are_empty_too(self) -> None:
        """No model points *at* ``audit_logs`` either.

        A reverse relation would be just as bad: it would mean some module had
        made an audit row part of its own object graph, and ``audit``'s
        independence would be a claim about the forward direction only.
        """
        assert not AuditLog._meta.related_objects


class TestOccurredAt:
    """The timestamp is supplied, not stamped on."""

    def test_occurred_at_is_not_auto_now_add(self) -> None:
        """``auto_now_add`` would ignore a caller-supplied value.

        A replayed or imported event carries the time it happened. Combined with
        the immutability trigger, ``auto_now_add`` would make a replayed event
        permanently mis-timestamped with no way to correct it — the trigger
        makes the mistake uneditable, so the default has to be non-destructive.
        """
        field = AuditLog._meta.get_field("occurred_at")
        assert isinstance(field, DateTimeField)
        assert field.auto_now_add is False
        assert field.auto_now is False
        assert field.has_default() is True


class TestColumnsMatchTheDesign:
    """03 §12.1 read literally, including which columns are nullable."""

    def test_action_width_matches_03(self) -> None:
        field = AuditLog._meta.get_field("action")
        assert isinstance(field, CharField)
        assert field.max_length == ACTION_MAX_LENGTH == 64
        assert field.null is False

    def test_user_agent_is_nullable(self) -> None:
        """03 §12.1 marks ``user_agent`` NULL.

        Nullable rather than default-empty because "the client sent no
        User-Agent" and "we did not record one" are different facts, and only
        one of them is a defect worth chasing.
        """
        field = AuditLog._meta.get_field("user_agent")
        assert field.null is True

    def test_ip_address_is_nullable_and_a_real_ip_type(self) -> None:
        field = AuditLog._meta.get_field("ip_address")
        assert isinstance(field, GenericIPAddressField)
        assert field.null is True

    def test_the_change_columns_are_json(self) -> None:
        for name in ("previous_value", "new_value"):
            field = AuditLog._meta.get_field(name)
            assert isinstance(field, JSONField), name
            assert field.null is True, name

    def test_primary_key_is_big(self) -> None:
        """An audit table is the one that grows without bound.

        ``BigAutoField`` comes from ``AuditConfig.default_auto_field``; an app
        without an ``apps.py`` silently gets Django's global default instead, and
        a 32-bit key on the fastest-growing table is not something that announces
        itself.
        """
        assert isinstance(AuditLog._meta.pk, BigAutoField)

    def test_actor_type_vocabulary_is_the_documented_one(self) -> None:
        """Three members, and no default (``apps.audit.constants``).

        A fourth member would be a new way to say "the system did it", which is
        the misattribution an audit trail cannot recover from.
        """
        assert set(ActorType.values) == {"USER", "SYSTEM", "ADMIN"}


class TestMeta:
    def test_table_name_is_the_documented_one(self) -> None:
        assert AuditLog._meta.db_table == "audit_logs"

    def test_newest_first_by_default(self) -> None:
        """An audit table is read by asking what happened around then."""
        assert AuditLog._meta.ordering == ("-occurred_at", "-id")

    def test_all_four_indexes_of_03_are_declared(self) -> None:
        names = {index.name for index in AuditLog._meta.indexes}
        assert names == {
            "audit_object_time_idx",
            "audit_actor_time_idx",
            "audit_action_time_idx",
            "audit_time_idx",
        }

    def test_the_ordering_relevant_indexes_descend_on_time(self) -> None:
        """A b-tree can scan backwards, so direction is free — but the three
        investigation indexes exist to serve "newest first" queries, and a later
        editor dropping the ``-`` would leave the intent invisible."""
        by_name = {index.name: index for index in AuditLog._meta.indexes}
        for name in (
            "audit_object_time_idx",
            "audit_actor_time_idx",
            "audit_action_time_idx",
        ):
            assert by_name[name].fields[-1] == "-occurred_at", name
        # 03 §12.1 writes the fourth as plain `(occurred_at)`.
        assert by_name["audit_time_idx"].fields == ["occurred_at"]

    def test_constraint_names(self) -> None:
        names = {constraint.name for constraint in AuditLog._meta.constraints}
        assert names == {
            "audit_logs_actor_type_valid",
            "audit_logs_action_not_empty",
            "audit_logs_object_type_not_empty",
        }

    # What the three constraints *do* is asserted against PostgreSQL in
    # ``tests/integration/test_audit_constraints.py`` rather than by reading the
    # ``Q`` objects back here. Reading them would assert that the constraint is
    # spelled a particular way; inserting a bad row asserts that it works, and
    # only the second survives a change in how Django renders a ``Q``.


class TestStrCarriesNoPii:
    """``__str__`` reaches logs, admin dropdowns and tracebacks."""

    def test_object_repr_is_not_in_the_representation(self) -> None:
        """``object_repr`` can hold a member's name.

        Those surfaces have no retention policy and no access control, so a
        member's name in ``__str__`` is a member's name in every log aggregator
        the platform ships to. The identifiers are enough to find the row.
        """
        entry = AuditLog(
            action="user.suspended",
            object_type="user",
            object_id="3f9c1a52-0000-4000-8000-000000000001",
            object_repr="Chinedu Okafor",
        )
        rendered = str(entry)
        assert "Chinedu" not in rendered
        assert "Okafor" not in rendered
        assert "user.suspended" in rendered
        assert "3f9c1a52-0000-4000-8000-000000000001" in rendered


class TestDeleteIsRefused:
    """FR-AUD-003's application-side half. The trigger is the other half."""

    def test_delete_raises_even_for_an_unsaved_instance(self) -> None:
        """Raising before any database work is deliberate.

        The database trigger would refuse the DELETE anyway; this exists so the
        ordinary mistake produces a message that names the rule instead of a
        psycopg error surfacing from inside an unrelated test.
        """
        with pytest.raises(RuntimeError, match="append-only"):
            AuditLog(action="user.suspended", object_type="user", object_id="1").delete()
