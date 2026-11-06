"""Group structure, explicit role templates and platform assignments."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.db.models import Q

from apps.groups.constants import GroupStatus, MembershipMode, MemberStatus


class RoleTemplate(models.Model):
    code = models.CharField(max_length=48, unique=True)
    name = models.CharField(max_length=100)
    scope = models.CharField(max_length=16, choices=[("GROUP", "Group"), ("PLATFORM", "Platform")])
    policies = models.JSONField(
        default=dict, help_text="Frozen safety bounds: required, optional or dual."
    )

    class Meta:
        db_table = "role_templates"

    def __str__(self) -> str:
        return self.code


class RoleSet(models.Model):
    code = models.CharField(max_length=48, unique=True)
    roles = models.ManyToManyField(RoleTemplate, related_name="role_sets")

    class Meta:
        db_table = "role_sets"

    def __str__(self) -> str:
        return self.code


class GroupType(models.Model):
    code = models.CharField(max_length=48, unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    is_system = models.BooleanField(default=False)
    default_role_set = models.ForeignKey(RoleSet, on_delete=models.PROTECT)

    class Meta:
        db_table = "group_types"

    def __str__(self) -> str:
        return self.name


class Group(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    logo = models.CharField(max_length=500, blank=True, default="")
    group_type = models.ForeignKey(GroupType, on_delete=models.PROTECT)
    location = models.ForeignKey(
        "locations.Location", on_delete=models.PROTECT, related_name="hosted_groups"
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_groups"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_groups"
    )
    status = models.CharField(
        max_length=16, choices=GroupStatus.choices, default=GroupStatus.ACTIVE
    )
    rules = models.TextField(blank=True, default="")
    membership_mode = models.CharField(
        max_length=16, choices=MembershipMode.choices, default=MembershipMode.INVITE_ONLY
    )
    requires_membership_approval = models.BooleanField(default=True)
    permission_version = models.PositiveBigIntegerField(default=0, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "groups"
        indexes = [
            models.Index(fields=["location", "status"], name="groups_location_status_idx"),
            GinIndex(fields=["name"], opclasses=["gin_trgm_ops"], name="groups_name_trgm_idx"),
        ]
        constraints = [models.CheckConstraint(condition=~Q(name=""), name="groups_name_not_empty")]

    def __str__(self) -> str:
        return f"Group({self.public_id})"


class GroupMember(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    group = models.ForeignKey(Group, on_delete=models.PROTECT, related_name="members")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="memberships"
    )
    status_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="membership_status_changes",
    )
    status = models.CharField(
        max_length=24, choices=MemberStatus.choices, default=MemberStatus.PENDING_APPROVAL
    )
    joined_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_memberships",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)
    suspension_reason = models.TextField(blank=True, default="")
    left_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "group_members"
        constraints = [
            models.UniqueConstraint(fields=["group", "user"], name="group_members_user_uniq")
        ]
        indexes = [models.Index(fields=["user", "status"], name="group_members_user_status_idx")]

    def __str__(self) -> str:
        return f"GroupMember({self.public_id})"


class GroupMemberHistory(models.Model):
    group_member = models.ForeignKey(GroupMember, on_delete=models.PROTECT, related_name="history")
    from_status = models.CharField(max_length=24, blank=True, default="")
    to_status = models.CharField(max_length=24)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.TextField(blank=True, default="")
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "group_member_history"
        indexes = [
            models.Index(fields=["group_member", "occurred_at"], name="member_history_time_idx")
        ]

    def __str__(self) -> str:
        return f"GroupMemberHistory({self.pk})"


class Permission(models.Model):
    code = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    category = models.CharField(max_length=48)
    is_system = models.BooleanField(default=True)
    is_write = models.BooleanField(default=False)
    dual_control = models.BooleanField(default=False)

    class Meta:
        db_table = "permissions"

    def __str__(self) -> str:
        return self.code


class GroupRole(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    code = models.CharField(max_length=48)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    group = models.ForeignKey(
        Group, on_delete=models.PROTECT, null=True, blank=True, related_name="roles"
    )
    template = models.ForeignKey(RoleTemplate, on_delete=models.PROTECT)
    is_system = models.BooleanField(default=False)
    is_configurable = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "group_roles"
        constraints = [
            models.UniqueConstraint(
                fields=["group", "code"], nulls_distinct=False, name="group_roles_code_uniq"
            )
        ]

    def __str__(self) -> str:
        return f"GroupRole({self.public_id})"


class GroupPermission(models.Model):
    group_role = models.ForeignKey(
        GroupRole, on_delete=models.CASCADE, related_name="permission_grants"
    )
    permission = models.ForeignKey(Permission, on_delete=models.PROTECT)
    granted = models.BooleanField(default=False)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="group_permission_changes",
    )

    class Meta:
        db_table = "group_permissions"
        constraints = [
            models.UniqueConstraint(
                fields=["group_role", "permission"], name="group_permissions_uniq"
            )
        ]

    def __str__(self) -> str:
        return f"GroupPermission({self.pk})"


class GroupMemberRole(models.Model):
    group_member = models.ForeignKey(
        GroupMember, on_delete=models.PROTECT, related_name="role_assignments"
    )
    group_role = models.ForeignKey(
        GroupRole, on_delete=models.PROTECT, related_name="member_assignments"
    )
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    assigned_at = models.DateTimeField(auto_now_add=True)
    bootstrap = models.BooleanField(default=False, editable=False)

    class Meta:
        db_table = "group_member_roles"
        constraints = [
            models.UniqueConstraint(
                fields=["group_member", "group_role"], name="group_member_roles_uniq"
            )
        ]

    def __str__(self) -> str:
        return f"GroupMemberRole({self.pk})"


class PlatformRoleAssignment(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="platform_role_assignments"
    )
    role = models.ForeignKey(
        GroupRole, on_delete=models.PROTECT, related_name="platform_assignments"
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="platform_roles_assigned",
    )
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "platform_role_assignments"
        constraints = [
            models.UniqueConstraint(fields=["user", "role"], name="platform_role_user_uniq")
        ]

    def __str__(self) -> str:
        return f"PlatformRoleAssignment({self.pk})"


class PermissionState(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    version = models.PositiveBigIntegerField(default=0)

    class Meta:
        db_table = "platform_permission_state"
        constraints = [models.CheckConstraint(condition=Q(id=1), name="permission_state_singleton")]

    def __str__(self) -> str:
        return f"PermissionState({self.version})"


class GroupInvitation(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    group = models.ForeignKey(Group, on_delete=models.PROTECT, related_name="invitations")
    invited_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="group_invitations",
    )
    invited_phone = models.CharField(max_length=20, null=True, blank=True)
    invited_email = models.EmailField(null=True, blank=True)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sent_group_invitations"
    )
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    declined_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "group_invitations"
        constraints = [
            models.CheckConstraint(
                condition=Q(invited_user__isnull=False)
                | (Q(invited_phone__isnull=False) & ~Q(invited_phone=""))
                | (Q(invited_email__isnull=False) & ~Q(invited_email="")),
                name="group_invitation_target",
            ),
            models.CheckConstraint(
                condition=Q(accepted_at__isnull=True) | Q(declined_at__isnull=True),
                name="group_invitation_decision",
            ),
            models.CheckConstraint(
                condition=Q(expires_at__gt=models.F("created_at")), name="group_invitation_expiry"
            ),
        ]

    def __str__(self) -> str:
        return f"GroupInvitation({self.public_id})"
