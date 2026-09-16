"""``audit.record()`` — the only path into ``audit_logs``, against real PostgreSQL.

The behaviour under test is mostly refusal. ``record()`` is a narrow gate for a
reason: everything that writes an audit entry writes it through here, so a rule
enforced here is enforced everywhere, and a rule missed here is missed
everywhere. Reading the rules as tests rather than as prose is also how the two
columns that must never disagree — ``actor_type`` and ``actor_user_id`` — are
kept in step.

The transaction requirement is tested with ``django_db(transaction=True)``,
which does not wrap the test in a transaction. In autocommit mode the entry would
commit *alone* — surviving the rollback of the action it claims to describe —
and that is the failure this rule exists to prevent, so it is worth the one test
that has to leave the usual harness.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import transaction

from apps.audit.constants import ActorType
from apps.audit.models import (
    OBJECT_ID_MAX_LENGTH,
    OBJECT_REPR_MAX_LENGTH,
    USER_AGENT_MAX_LENGTH,
    AuditLog,
)
from apps.audit.services import AuditContext, record
from tests.factories.accounts import UserFactory
from tests.factories.audit import audit_values

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


class TestWritesTheEntry:
    """FR-AUD-001's list: who, what, when, which object, before, after."""

    def test_records_a_system_action(self) -> None:
        entry = record(**audit_values())

        assert entry.pk is not None
        assert entry.action == "contribution.recorded"
        assert entry.actor_type == ActorType.SYSTEM
        assert entry.actor_user_id is None
        assert entry.object_type == "contribution"
        assert entry.object_id == "11111111-1111-4111-8111-111111111111"
        assert entry.occurred_at is not None

    def test_records_before_and_after(self) -> None:
        """FR-AUD-002's whole value is here.

        "Status changed" answers no question an investigation asks; "ACTIVE to
        SUSPENDED" answers the only one it does.
        """
        entry = record(
            **audit_values(
                action="user.status_changed",
                previous_value={"account_status": "ACTIVE"},
                new_value={"account_status": "SUSPENDED"},
            )
        )
        assert entry.previous_value == {"account_status": "ACTIVE"}
        assert entry.new_value == {"account_status": "SUSPENDED"}

    def test_records_a_named_actor(self) -> None:
        member = UserFactory()
        entry = record(
            **audit_values(
                action="user.suspended",
                actor_type=ActorType.USER,
                actor_user_id=member.pk,
            )
        )
        assert entry.actor_user_id == member.pk

    def test_the_row_is_readable_after_the_fact(self) -> None:
        """The entry survives its own function returning — nothing is deferred."""
        record(**audit_values())
        assert AuditLog.objects.filter(action="contribution.recorded").count() == 1

    def test_context_is_recorded(self) -> None:
        entry = record(
            **audit_values(
                context=AuditContext(
                    ip_address="203.0.113.7",
                    user_agent="ihweza-android/1.0",
                    device_id=7,
                    session_id=12,
                )
            )
        )
        assert entry.ip_address == "203.0.113.7"
        assert entry.user_agent == "ihweza-android/1.0"
        assert entry.device_id == 7
        assert entry.session_id == 12

    def test_absent_context_leaves_the_columns_null(self) -> None:
        """A scheduled job has no request, and null is the honest value."""
        entry = record(**audit_values())
        assert entry.ip_address is None
        assert entry.user_agent is None
        assert entry.device_id is None
        assert entry.session_id is None


class TestActionName:
    """The action column is the only handle on "who has done this before?"."""

    def test_rejects_prose(self) -> None:
        with pytest.raises(ValueError, match=r"noun\.verb_past"):
            record(**audit_values(action="User was suspended by an admin"))

    def test_rejects_a_single_segment(self) -> None:
        with pytest.raises(ValueError, match=r"noun\.verb_past"):
            record(**audit_values(action="suspended"))

    def test_rejects_uppercase(self) -> None:
        """``USER.SUSPENDED`` and ``user.suspended`` are different strings.

        Storing both would split one history across two filters, and neither the
        admin filter nor the incident query would find the whole of it.
        """
        with pytest.raises(ValueError, match=r"noun\.verb_past"):
            record(**audit_values(action="USER.SUSPENDED"))

    def test_rejects_a_leading_underscore_segment(self) -> None:
        with pytest.raises(ValueError, match=r"noun\.verb_past"):
            record(**audit_values(action="user._suspended"))

    def test_rejects_a_trailing_dot(self) -> None:
        with pytest.raises(ValueError, match=r"noun\.verb_past"):
            record(**audit_values(action="user.suspended."))

    def test_accepts_a_three_segment_name(self) -> None:
        """Depth is allowed: ``disbursement.separation_of_duties.refused`` names
        the rule that fired, which is the fact an investigator wants."""
        entry = record(**audit_values(action="disbursement.separation_of_duties.refused"))
        assert entry.action == "disbursement.separation_of_duties.refused"

    def test_rejects_a_name_longer_than_the_column(self) -> None:
        with pytest.raises(ValueError, match="column holds"):
            record(**audit_values(action="a." + "b" * 100))


class TestActorTypeAndActorAgree:
    """Two columns holding one fact, checked against each other.

    The redundancy is unavoidable — 03 §12.1 has both — so the useful thing to do
    with it is refuse the states where they disagree, before they are written and
    therefore before they are permanent.
    """

    def test_system_actor_must_not_name_a_user(self) -> None:
        """Otherwise a human action gets filed as an automated one.

        That is the one misattribution an audit trail cannot recover from: the
        row is immutable, and the reader has no reason to doubt it.
        """
        member = UserFactory()
        with pytest.raises(ValueError, match="SYSTEM"):
            record(
                **audit_values(
                    actor_type=ActorType.SYSTEM,
                    actor_user_id=member.pk,
                )
            )

    def test_user_actor_must_name_a_user(self) -> None:
        with pytest.raises(ValueError, match="must name the user"):
            record(**audit_values(actor_type=ActorType.USER))

    def test_admin_actor_must_name_a_user(self) -> None:
        with pytest.raises(ValueError, match="must name the user"):
            record(**audit_values(actor_type=ActorType.ADMIN))

    def test_admin_actor_with_a_user_is_accepted(self) -> None:
        member = UserFactory()
        entry = record(
            **audit_values(
                actor_type=ActorType.ADMIN,
                actor_user_id=member.pk,
            )
        )
        assert entry.actor_type == ActorType.ADMIN


class TestValuesMustBeStorable:
    """``JSONField`` uses plain ``json.dumps``, with no custom encoder.

    In this project that is not hypothetical: money is ``Decimal`` throughout, so
    the natural call — ``new_value={"amount": contribution.amount}`` — would abort
    the business action at its last statement, after the money had moved.
    """

    def test_rejects_a_decimal(self) -> None:
        with pytest.raises(ValueError, match="JSON primitives"):
            record(**audit_values(new_value={"amount": Decimal("500.00")}))

    def test_rejects_a_queryset_or_model(self) -> None:
        member = UserFactory()
        with pytest.raises(ValueError, match="JSON primitives"):
            record(**audit_values(new_value={"user": member}))

    def test_names_the_offending_field(self) -> None:
        """The message has to say *which* of the two snapshots was wrong."""
        with pytest.raises(ValueError, match="previous_value"):
            record(**audit_values(previous_value={"when": object()}))

    def test_accepts_money_as_a_decimal_string(self) -> None:
        """The same convention the API speaks (04 §1.1).

        A float would put a binary rounding error into the permanent record of a
        cash handover, which is exactly what the integer-minor-units rule exists
        to prevent.
        """
        entry = record(**audit_values(new_value={"amount": "500.00", "currency": "NGN"}))
        assert entry.new_value == {"amount": "500.00", "currency": "NGN"}

    def test_accepts_nested_structures(self) -> None:
        entry = record(**audit_values(new_value={"roles": ["treasurer"], "limits": {"max": 50000}}))
        assert entry.new_value == {"roles": ["treasurer"], "limits": {"max": 50000}}


class TestObjectReference:
    """Identifiers fail loudly; descriptions truncate."""

    def test_integer_object_ids_are_stringified(self) -> None:
        """The column is generic, so it has to hold both kinds of identifier
        (03 §1.1: internal integers never leave the process, so public ids are
        the usual value — but a table without a public id still has to be
        nameable)."""
        entry = record(**audit_values(object_type="otp_code", object_id=918))
        assert entry.object_id == "918"

    def test_an_over_long_object_id_raises_rather_than_truncating(self) -> None:
        """A truncated identifier points at a different object.

        An audit entry that confidently names the wrong member is worse than one
        that was never written.
        """
        with pytest.raises(ValueError, match="object_id"):
            record(**audit_values(object_id="9" * (OBJECT_ID_MAX_LENGTH + 1)))

    def test_an_empty_object_id_raises(self) -> None:
        with pytest.raises(ValueError, match="object_id"):
            record(**audit_values(object_id=""))

    def test_an_empty_object_type_raises(self) -> None:
        with pytest.raises(ValueError, match="object_type"):
            record(**audit_values(object_type=""))

    def test_object_repr_truncates_rather_than_failing(self) -> None:
        """A long label must not be able to abort a member's contribution.

        Nothing depends on ``object_repr`` being complete — it is a memory aid —
        so losing a tail is the cheaper failure, unlike an identifier.
        """
        entry = record(**audit_values(object_repr="x" * (OBJECT_REPR_MAX_LENGTH + 500)))
        assert len(entry.object_repr) == OBJECT_REPR_MAX_LENGTH

    def test_a_long_user_agent_truncates_rather_than_failing(self) -> None:
        """The client chooses this header. It must not be able to refuse a write."""
        entry = record(**audit_values(context=AuditContext(user_agent="A" * 5000)))
        assert entry.user_agent is not None
        assert len(entry.user_agent) == USER_AGENT_MAX_LENGTH


class TestTransactionRequirement:
    """The entry has to commit with the action, or roll back with it.

    Marked ``transaction=True`` for the first test, which is the *opposite* of
    the usual harness: every other test here runs inside a transaction, and this
    one has to prove what happens when nothing does. pytest-django resolves the
    marker nearest the test, so the class-level marker wins over the module's.
    """

    pytestmark = pytest.mark.django_db(transaction=True)

    def test_refuses_to_run_in_autocommit_mode(self) -> None:
        """Otherwise the entry outlives a rolled-back action.

        A record of something that never happened is indistinguishable from a
        record of something that did, and that is the state that makes an audit
        trail actively misleading rather than merely incomplete.

        Asserted first, because if the harness ever stopped leaving this test in
        autocommit mode the test would pass while checking nothing.
        """
        assert transaction.get_autocommit() is True
        with pytest.raises(RuntimeError, match="inside a transaction"):
            record(**audit_values())


class TestTheEntryJoinsTheCallersTransaction:
    """The positive half of the rule above, inside the normal harness."""

    def test_an_entry_rolls_back_with_its_action(self) -> None:
        """Raising after the write must take the audit entry with it.

        If the entry were written on a separate connection, or committed early,
        this is where it would show — and a "recorded" action that never happened
        would be sitting in the table looking exactly like one that did.
        """
        before = AuditLog.objects.count()
        with pytest.raises(_DeliberateFailure):
            with transaction.atomic():
                record(**audit_values(action="loan.approved"))
                raise _DeliberateFailure
        assert AuditLog.objects.count() == before
        assert not AuditLog.objects.filter(action="loan.approved").exists()


class _DeliberateFailure(Exception):
    """A failure that is the point of the test, not a bug in it."""
