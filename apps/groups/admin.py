"""Scoped read-only inspection; group mutations use the audited service API."""

from __future__ import annotations

from typing import Any, ClassVar, cast

from django.contrib import admin
from django.db.models import Model, QuerySet
from django.http import HttpRequest

from apps.accounts.models import User
from apps.groups.models import (
    Group,
    GroupInvitation,
    GroupMember,
    GroupMemberHistory,
    GroupMemberRole,
    GroupPermission,
    GroupRole,
    GroupType,
    Permission,
    PlatformRoleAssignment,
    RoleTemplate,
)
from apps.groups.permissions import has_perm
from apps.locations import admin as location_admin
from apps.locations.models import Country, Currency, Location, LocationType


class GroupInspectionAdmin(admin.ModelAdmin):
    actions = None
    group_path: ClassVar[str] = "group"
    permission_code: ClassVar[str] = "groups.view"

    def get_queryset(self, request: HttpRequest) -> QuerySet[Any]:
        rows = super().get_queryset(request)
        user = cast(User, request.user)
        if has_perm(user, None, self.permission_code):
            return rows
        if self.group_path == "":
            groups = rows
        else:
            groups = Group.objects.filter(
                pk__in=rows.values_list(f"{self.group_path}_id", flat=True)
            )
        allowed = [g.pk for g in groups if has_perm(user, g, self.permission_code)]
        lookup = "pk__in" if self.group_path == "" else f"{self.group_path}__in"
        return rows.filter(**{lookup: allowed})

    def has_module_permission(self, request: HttpRequest) -> bool:
        return self.get_queryset(request).exists()

    def has_view_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        rows = self.get_queryset(request)
        return rows.filter(pk=obj.pk).exists() if obj is not None else rows.exists()

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False


@admin.register(Group)
class GroupAdmin(GroupInspectionAdmin):
    group_path = ""
    list_display = ("public_id", "name", "status", "group_type", "created_at")


@admin.register(GroupMember)
class GroupMemberAdmin(GroupInspectionAdmin):
    permission_code = "members.view"
    list_display = ("public_id", "group", "user", "status", "joined_at")


@admin.register(GroupMemberHistory)
class GroupHistoryAdmin(GroupInspectionAdmin):
    group_path = "group_member__group"
    permission_code = "members.view_history"
    list_display = ("group_member", "from_status", "to_status", "occurred_at")


@admin.register(GroupRole)
class GroupRoleAdmin(GroupInspectionAdmin):
    permission_code = "groups.manage_settings"
    list_display = ("public_id", "group", "code", "template")


@admin.register(GroupPermission)
class GroupPermissionAdmin(GroupInspectionAdmin):
    group_path = "group_role__group"
    permission_code = "groups.manage_settings"
    list_display = ("group_role", "permission", "granted")


@admin.register(GroupMemberRole)
class GroupAssignmentAdmin(GroupInspectionAdmin):
    group_path = "group_member__group"
    permission_code = "members.view"
    list_display = ("group_member", "group_role", "assigned_at")


@admin.register(GroupInvitation)
class InvitationAdmin(GroupInspectionAdmin):
    permission_code = "members.invite"
    exclude = ("token_hash", "invited_phone", "invited_email")
    list_display = ("public_id", "group", "expires_at", "accepted_at", "declined_at")


class CatalogueInspectionAdmin(GroupInspectionAdmin):
    permission_code = "platform.manage_group_types"

    def get_queryset(self, request: HttpRequest) -> QuerySet[Any]:
        rows = admin.ModelAdmin.get_queryset(self, request)
        return (
            rows if has_perm(cast(User, request.user), None, self.permission_code) else rows.none()
        )


@admin.register(GroupType, RoleTemplate, Permission)
class CatalogueAdmin(CatalogueInspectionAdmin):
    list_display = ("code", "name")


@admin.register(PlatformRoleAssignment)
class PlatformAssignmentAdmin(CatalogueInspectionAdmin):
    permission_code = "platform.manage_users"
    list_display = ("user", "role", "assigned_at")


class LocationInspectionAdmin(CatalogueInspectionAdmin):
    permission_code = "platform.manage_locations"


# L1 adds authorization to the L0 registry without a reverse module dependency.
for location_model in (Country, Currency, Location, LocationType):
    admin.site.unregister(location_model)


@admin.register(Country)
class PlatformCountryAdmin(LocationInspectionAdmin, location_admin.CountryAdmin):
    pass


@admin.register(Currency)
class PlatformCurrencyAdmin(LocationInspectionAdmin, location_admin.CurrencyAdmin):
    pass


@admin.register(Location)
class PlatformLocationAdmin(LocationInspectionAdmin, location_admin.LocationAdmin):
    pass


@admin.register(LocationType)
class PlatformLocationTypeAdmin(LocationInspectionAdmin, location_admin.LocationTypeAdmin):
    pass
