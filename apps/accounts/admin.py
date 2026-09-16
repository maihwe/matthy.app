"""Django admin registration for ``accounts``.

This is the **internal** surface — the operator tool, not the member-facing one.
Two rules apply throughout:

* **No credential is displayed or editable.** ``password`` and ``pin_hash`` are
  read-only hash fields, and no code is shown at all. An admin screen that can
  set a member's password is an admin screen whose access log matters as much
  as the ledger's (10 §2).
* **PII stays out of list views.** ``__str__`` returns the public ID, and the
  list columns are the public ID, the status and the timestamps — the fields an
  operator actually searches on. A member's phone number in a default list view
  is a phone number in every screenshot of it.

A plain ``ModelAdmin`` is used rather than ``django.contrib.auth.admin.UserAdmin``
because ``UserAdmin`` is built around a stored ``is_active`` boolean and a
username/password creation form. This model has neither: ``is_active`` is
derived from ``account_status`` and the login identifier is ``phone``. Fighting
``UserAdmin`` into that shape produces a screen with fields that do not apply.

Finally, the admin classes are **not** subscripted (``admin.ModelAdmin[User]``).
Django's admin classes are generic in django-stubs but not at runtime —
``ModelAdmin`` has no ``__class_getitem__`` — so the subscript raises
``TypeError`` the moment ``admin.autodiscover()`` imports this module, which is
every management command, in every environment including production. Making it
work would mean running ``django_stubs_ext``, a type-checking shim, in
production. Nothing here needs it: every ``list_display`` entry is a plain
string and every ``has_*_permission`` is annotated explicitly, so the model
binding the subscript would add is unused.
"""

from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from apps.accounts.models import (
    Device,
    OTPCode,
    User,
    UserProfile,
    UserSession,
    VerificationToken,
)


class UserProfileInline(admin.StackedInline):
    """Stacked rather than tabular: it holds an address and next-of-kin, which
    do not read as a table row."""

    model = UserProfile
    extra = 0
    can_delete = False
    verbose_name_plural = "Profile"


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = (
        "public_id",
        "full_name",
        "account_status",
        "verification_status",
        "is_phone_verified",
        "date_registered",
    )
    list_filter = (
        "account_status",
        "verification_status",
        "is_phone_verified",
        "is_email_verified",
        "two_factor_enabled",
        "is_staff",
    )
    search_fields = ("full_name", "phone", "email", "public_id")
    ordering = ("-date_registered",)
    readonly_fields = (
        "public_id",
        "password",
        "pin_hash",
        "date_registered",
        "last_login",
    )
    inlines = (UserProfileInline,)

    fieldsets = (
        (
            None,
            {
                "fields": ("public_id", "full_name", "phone", "email", "username"),
                "description": (
                    "Phone is the login identifier and is unique. Email is "
                    "optional and unique case-insensitively."
                ),
            },
        ),
        (
            "Status",
            {
                "fields": (
                    "account_status",
                    "verification_status",
                    "is_phone_verified",
                    "is_email_verified",
                    "failed_login_attempts",
                    "locked_until",
                ),
                # is_active is absent on purpose: it is derived from
                # account_status, and an editable copy is a copy that can
                # disagree with the column that authentication actually reads.
                "description": (
                    "Suspending an account blocks authentication. It does not "
                    "delete anything — §44 rule 10 requires the history to stay."
                ),
            },
        ),
        (
            "Authentication",
            {
                "fields": ("password", "pin_hash"),
                "description": (
                    "Hashes only, and read-only. Neither a password nor a PIN "
                    "can be set or read here; a member sets their own, and an "
                    "operator who could set one could impersonate them."
                ),
            },
        ),
        (
            "Two-factor",
            {"fields": ("two_factor_enabled", "two_factor_method")},
        ),
        (
            "Permissions",
            {
                "fields": ("is_staff", "is_superuser", "groups", "user_permissions"),
                "description": (
                    "Group-scoped financial permissions are NOT Django groups. "
                    "They live in group_permissions and are checked through "
                    "has_perm(user, group, code) — 05 §8."
                ),
            },
        ),
        (
            "Dates",
            {"fields": ("date_registered", "last_login")},
        ),
    )


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    # 03 §4.3 gives `devices` no public_id — a device is only ever addressed
    # by its member, so there is no URL that needs an opaque identifier.
    list_display = ("id", "user", "platform", "model", "last_seen_at", "is_revoked")
    list_filter = ("platform", "is_revoked")
    search_fields = ("device_fingerprint", "user__phone", "user__public_id")
    readonly_fields = ("created_at",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Devices register themselves from the app.

        An operator-created device row would be a device the member never
        registered and cannot recognise in their session list — which is worse
        than no row, because it looks legitimate.
        """
        return False


@admin.register(UserSession)
class UserSessionAdmin(admin.ModelAdmin):
    list_display = ("public_id", "user", "issued_at", "expires_at", "revoked_at", "last_used_at")
    list_filter = ("revoked_at",)
    search_fields = ("public_id", "user__phone", "user__public_id")
    readonly_fields = (
        "public_id",
        "user",
        "device",
        "refresh_token_hash",
        "issued_at",
        "expires_at",
        "revoked_at",
        "last_used_at",
        "ip_address",
        "user_agent",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Sessions are issued by the authentication service, never by hand."""
        return False


@admin.register(OTPCode)
class OTPCodeAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "purpose",
        "destination",
        "attempts",
        "max_attempts",
        "expires_at",
        "consumed_at",
    )
    list_filter = ("purpose",)
    search_fields = ("destination",)
    readonly_fields = ("code_hash", "created_at")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: OTPCode | None = None) -> bool:
        """Read-only.

        An operator who can edit an OTP row can extend its expiry or reset its
        attempt counter, which defeats the rate limit that makes a 6-digit code
        safe. The screen is for investigating abuse, not for intervening.
        """
        return False


@admin.register(VerificationToken)
class VerificationTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "purpose", "expires_at", "consumed_at", "created_at")
    list_filter = ("purpose",)
    search_fields = ("user__phone", "user__public_id")
    readonly_fields = ("token_hash", "created_at")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False
