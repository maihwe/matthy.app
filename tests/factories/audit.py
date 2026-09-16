"""Factories for the ``audit`` module.

There is no ``SubFactory`` for the actor, because ``actor_user_id`` is not a
ForeignKey — see ``apps.audit.models``. A test that wants a human actor passes
the id explicitly, which keeps the deliberate absence of the FK visible at the
call site rather than hidden behind a factory that looks like every other one::

    member = UserFactory()
    AuditLogFactory(actor_type=ActorType.USER, actor_user_id=member.pk)

The default actor is ``SYSTEM`` with no user id, because that is the shape that
needs no other object to exist, and a factory that quietly created a user would
make every audit test pay for a row it does not use.

Rows are created with ``objects.create`` rather than through
``apps.audit.services.record``. That is intentional: these factories populate the
table for *read* tests, and the validation in ``record()`` is itself under test —
routing around it here means a test of ``record()`` cannot pass because a factory
already satisfied the rule it is checking.
"""

from __future__ import annotations

from typing import Any

import factory
from factory.django import DjangoModelFactory

from apps.audit.constants import ActorType
from apps.audit.models import AuditLog


class AuditLogFactory(DjangoModelFactory[AuditLog]):
    """One recorded action, by default an automated one.

    ``action`` follows the ``noun.verb_past`` form that ``record()`` enforces, so
    a test asserting on filtering gets the same strings production writes.
    """

    class Meta:
        model = AuditLog

    action = "session.expired"
    actor_type = ActorType.SYSTEM
    actor_user_id = None

    object_type = "user_session"
    object_id = factory.Sequence(lambda n: f"00000000-0000-4000-8000-{n:012d}")
    object_repr = ""

    previous_value = None
    new_value = None

    ip_address = "197.210.0.1"
    user_agent = "ihweza-tests/1.0"
    device_id = None
    session_id = None

    # `occurred_at` is left to the model default (timezone.now). A factory that
    # froze it would make every row in a test share one timestamp, and the
    # ordering the table's four indexes exist to serve would go untested.


class UserAuditLogFactory(AuditLogFactory):
    """An action taken by a named person.

    Created with an actor id already supplied, since a ``USER`` entry without one
    is a row ``record()`` refuses to write — see ``TestActorTypeAndActorAgree``
    in ``tests/unit/test_audit_services.py``. The id is a plain integer because
    the column is; the referenced user may or may not exist, and for tests that
    read the table it does not need to.
    """

    action = "user.suspended"
    actor_type = ActorType.USER
    actor_user_id = factory.Sequence(lambda n: 10_000 + n)


def audit_values(**overrides: Any) -> dict[str, Any]:
    """Keyword arguments for ``record()``, with a valid baseline.

    Exists so that a test of one validation rule overrides exactly that one thing
    and cannot fail on a second rule it did not mean to exercise.
    """
    values: dict[str, Any] = {
        "action": "contribution.recorded",
        "actor_type": ActorType.SYSTEM,
        "object_type": "contribution",
        "object_id": "11111111-1111-4111-8111-111111111111",
        "object_repr": "Contribution 2026-09-16",
    }
    values.update(overrides)
    return values
