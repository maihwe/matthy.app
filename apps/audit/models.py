"""``audit_logs`` — 03 §12.1, FR-AUD-001…003, §44 rules 8 and 10.

**This model has no ForeignKeys, deliberately.** 03 names the columns
``actor_user_id``, ``device_id`` and ``session_id``, which is Django's shape for
a real FK, and every instinct says to make them one. Three reasons not to, and
they compound:

1. **A cascade must never be able to delete an audit row.** Devices are revoked
   and sessions expire; an ``ON DELETE CASCADE`` from either would erase the
   record of what happened during the window an investigator is asking about.
   FR-AUD-003 requires immutability, and a cascade is a deletion path that no
   trigger on this table can intercept.
2. **An audit record must outlive the thing it describes.** "Who approved this,
   and had they revoked that device?" has to be answerable *after* the device
   row is gone. That is what makes this an audit rather than an activity feed.
3. **It keeps ``audit`` at L0 with no dependencies.** Everything writes to it
   (02 §4.3). A module that imports half the codebase in order to define its own
   columns cannot be the one every layer is allowed to write to.

The cost is real and is accepted: referential integrity for ``actor_user_id``
is not enforced by the database. A reader resolving an actor gets a row or
nothing rather than an error, and a service that writes a bad id writes it
permanently. That is the price of not losing the record, and it is paid
knowingly.

``previous_value`` and ``new_value`` are what make a record explanatory rather
than merely indicative: "status changed" is not an audit entry, "status ACTIVE →
SUSPENDED" is.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar, NoReturn

from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.audit.constants import ActorType

__all__ = ["AuditLog"]

# 03 §12.1 caps `action` at 64. An action name is a stable identifier, not
# prose — `contribution.approved`, not a sentence.
ACTION_MAX_LENGTH = 64

# 03 §12.1 gives no width for the generic object reference. 64 holds a UUID's
# canonical form (36), the largest integer Django emits, and a composite key such
# as `scheme:1234`, with room to spare.
OBJECT_TYPE_MAX_LENGTH = 64
OBJECT_ID_MAX_LENGTH = 64

# 03 §12.1 gives no width. 512 is Django's own `User-Agent` ceiling and is enough
# for any real header; truncation is the caller's business, at the boundary.
USER_AGENT_MAX_LENGTH = 512

# 03 §12.1 gives no width. 255 is the `object_repr` convention Django's own
# admin history uses for the same job.
OBJECT_REPR_MAX_LENGTH = 255


class AuditLog(models.Model):
    """One immutable record of one action.

    There is no ``updated_at``, no ``is_deleted``, and no manager method that
    updates or deletes. ``save()` on an existing row raises rather than issuing
    an UPDATE, so a code path that means to correct a record fails legibly
    instead of by way of the database trigger's error.
    """

    actor_user_id = models.BigIntegerField(
        null=True,
        blank=True,
        default=None,
        # A plain integer, not an FK. See the module docstring. Named exactly as
        # Django would name a FK's column, so that a reader who assumes it is one
        # is not misled by an unexpected convention.
        help_text="Internal user id. No FK constraint, so the record outlives the user row.",
    )
    actor_type = models.CharField(max_length=16, choices=ActorType.choices)
    action = models.CharField(
        max_length=ACTION_MAX_LENGTH,
        help_text="Stable identifier, e.g. 'user.suspended'. Never prose.",
    )

    object_type = models.CharField(max_length=OBJECT_TYPE_MAX_LENGTH)
    object_id = models.CharField(
        max_length=OBJECT_ID_MAX_LENGTH,
        help_text="String form of the public id, or the primary key where there is none.",
    )
    object_repr = models.CharField(
        max_length=OBJECT_REPR_MAX_LENGTH,
        blank=True,
        default="",
        # A snapshot of how the object was named at the time. Redundant the
        # moment it is written, indispensable a year later, when the row it
        # described has been renamed or closed.
        help_text="How the object was named when this happened.",
    )

    previous_value = models.JSONField(null=True, blank=True, default=None)
    new_value = models.JSONField(null=True, blank=True, default=None)

    ip_address = models.GenericIPAddressField(null=True, blank=True, default=None)

    # 03 §12.1 marks `user_agent` NULL, so it is nullable rather than
    # default-empty: "no header was sent" and "we did not record one" are
    # different facts, and only one of them is a defect.
    user_agent = models.CharField(
        max_length=USER_AGENT_MAX_LENGTH,
        null=True,
        blank=True,
        default=None,
    )

    # Plain integers, for the same reason as actor_user_id.
    device_id = models.BigIntegerField(null=True, blank=True, default=None)
    session_id = models.BigIntegerField(null=True, blank=True, default=None)

    occurred_at = models.DateTimeField(
        default=timezone.now,
        # `default=` rather than `auto_now_add=`: a row replayed from an outbox
        # or imported from a legacy system must carry the time the event actually
        # happened. Because the trigger makes the row immutable after insert,
        # forcing `now` would leave a replayed event permanently mis-timestamped
        # with no way to correct it.
        help_text="When the action happened, not when the row was written.",
    )

    class Meta:
        db_table = "audit_logs"
        verbose_name = _("audit log entry")
        verbose_name_plural = _("audit log entries")
        # Newest first everywhere. An audit table is read by people asking "what
        # happened around then", and the default ordering should answer that.
        ordering = ("-occurred_at", "-id")
        indexes = [
            # 03 §12.1 names all four. Each serves a stated query rather than
            # being speculative:
            #   "what happened to this object?"       — the investigation query
            models.Index(
                fields=["object_type", "object_id", "-occurred_at"],
                name="audit_object_time_idx",
            ),
            #   "what did this person do?"            — insider review
            models.Index(
                fields=["actor_user_id", "-occurred_at"],
                name="audit_actor_time_idx",
            ),
            #   "who has suspended anyone this month?" — pattern search
            models.Index(
                fields=["action", "-occurred_at"],
                name="audit_action_time_idx",
            ),
            #   "everything in this window"           — the paginated browse.
            # Ascending, exactly as 03 §12.1 states: a b-tree scans either way,
            # so the direction is free and there is no reason to differ from the
            # document.
            models.Index(fields=["occurred_at"], name="audit_time_idx"),
        ]
        constraints = [
            # `actor_type` is a closed vocabulary (ActorType) and the column is
            # NOT NULL, so an IN check subsumes "not blank": an empty string is
            # not a member. `choices=` is validated by forms and serializers, not
            # by the ORM on save, so the database is where this has to hold.
            #
            # The member list is read from the enum, so adding a member without a
            # migration fails loudly here rather than being written and never
            # matching a filter.
            models.CheckConstraint(
                condition=Q(actor_type__in=ActorType.values),
                name="audit_logs_actor_type_valid",
            ),
            # An unnamed action cannot be searched for, and the action index is
            # the only way to ask "who has suspended anyone this month?".
            models.CheckConstraint(
                condition=~Q(action=""),
                name="audit_logs_action_not_empty",
            ),
            # A blank object_type would leave a row that the investigation index
            # cannot reach. A writer with no model to point at should still name
            # the domain it happened in.
            models.CheckConstraint(
                condition=~Q(object_type=""),
                name="audit_logs_object_type_not_empty",
            ),
        ]

    # Declared so the type checker binds `objects` to `Manager[AuditLog]`;
    # Django would supply an equivalent default.
    objects: ClassVar[models.Manager[AuditLog]] = models.Manager()

    def __str__(self) -> str:
        """Identifiers only.

        ``object_repr`` is tempting here and is wrong: it can hold a member's or
        a group's name, and ``__str__`` reaches logs, admin dropdowns and
        tracebacks — places with no retention policy and no access control.
        """
        return f"AuditLog({self.action}, {self.object_type}:{self.object_id})"

    def save(
        self,
        force_insert: bool = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        """Refuse to update an existing row.

        The database trigger is the actual control. This is the legible failure:
        a `RuntimeError` naming the problem beats psycopg's trigger exception
        surfacing from inside a test that was nominally about something else.

        Signature matches Django's documented ``Model.save`` so that mypy checks
        this as a real override rather than complaining about an incompatible one.
        """
        if self.pk is not None and not self._state.adding:
            raise RuntimeError(
                "audit_logs is append-only (FR-AUD-003). An existing entry cannot be "
                "modified; write a new entry describing the correction."
            )
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def delete(
        self,
        using: str | None = None,
        keep_parents: bool = False,
    ) -> NoReturn:
        """Refuse deletion from the ORM, for the same reason as ``save``.

        ``NoReturn`` rather than ``None``: the body cannot return, and the base
        class declares a return value, so ``None`` would be an incompatible
        override. It is also the honest type — every path raises.

        This does *not* cover ``AuditLog.objects.filter(...).delete()``, which
        never reaches this method. The trigger does, and that is why both exist.
        """
        raise RuntimeError(
            "audit_logs is append-only (FR-AUD-003). Entries are not deleted through "
            "the application; retention is a documented, out-of-band operation."
        )
