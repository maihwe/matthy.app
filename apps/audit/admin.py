"""Django admin registration for ``audit``.

**Read-only, and read-only by construction rather than by convention.** Add,
change and delete all return ``False``, so there is no form, no save button and
no bulk action — an operator cannot edit an audit record through the admin
because the admin never builds the screen that would let them.

This matters more here than anywhere else in the project. FR-AUD-003 and
SEC-AUD-* exist so that the record of who suspended a member, or who approved a
disbursement, is not editable by whoever has the most convenient access. An
admin screen that can amend it converts the whole table into an assertion by
whoever last held the mouse.

``object_repr`` is shown on the detail page but deliberately kept out of
``list_display``: it can hold a member's name, and a list view is a page that
gets screenshotted into a support ticket. The list columns are the identifiers
an operator is actually searching on.

The admin class is **not** subscripted (``admin.ModelAdmin[AuditLog]``) for the
reason given in ``apps.accounts.admin``: Django's admin classes are generic in
django-stubs but not at runtime, so the subscript is a ``TypeError`` at import,
in every environment. ``pyproject.toml`` exempts this module from
``disallow_any_generics`` to match.
"""

from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from apps.audit.models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "occurred_at",
        "actor_type",
        "actor_user_id",
        "action",
        "object_type",
        "object_id",
    )
    list_filter = ("actor_type", "action", "object_type")
    # Newest first, and the one column an investigator navigates by.
    date_hierarchy = "occurred_at"
    # `occurred_at` rather than the model Meta's ordering: the admin adds its own
    # tiebreaker, and naming it here keeps the SQL the admin issues visible.
    ordering = ("-occurred_at", "-id")
    search_fields = (
        "action",
        "object_type",
        "object_id",
        "object_repr",
        "actor_user_id",
    )
    readonly_fields = (
        "actor_user_id",
        "actor_type",
        "action",
        "object_type",
        "object_id",
        "object_repr",
        "previous_value",
        "new_value",
        "ip_address",
        "user_agent",
        "device_id",
        "session_id",
        "occurred_at",
    )
    fieldsets = (
        (
            None,
            {
                "fields": ("occurred_at", "actor_type", "actor_user_id"),
                "description": (
                    "Who acted, and when. A blank actor_user_id with "
                    "actor_type SYSTEM is an automated transition, not a "
                    "missing record."
                ),
            },
        ),
        (
            "Action",
            {"fields": ("action", "object_type", "object_id", "object_repr")},
        ),
        (
            "Change",
            {
                "fields": ("previous_value", "new_value"),
                "description": (
                    "These two are what make an entry explanatory rather than "
                    "merely indicative: 'status changed' is not an audit "
                    "record, 'status ACTIVE to SUSPENDED' is."
                ),
            },
        ),
        (
            "Context",
            {
                "fields": (
                    "ip_address",
                    "user_agent",
                    "device_id",
                    "session_id",
                )
            },
        ),
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Entries are written by the operation they describe, never by hand.

        A hand-written audit entry is a record of something that did not happen,
        which is worse than a missing one.
        """
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: AuditLog | None = None,
    ) -> bool:
        """No edit screen, at any level of privilege. FR-AUD-003."""
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: AuditLog | None = None,
    ) -> bool:
        """No deletion, including the bulk action.

        The database trigger would refuse the DELETE anyway (migration 0002), so
        this is what turns a 500 from a psycopg ``IntegrityError`` into an admin
        that simply does not offer the action.
        """
        return False
