"""The single permission resolution path; versioned cache with DB fallback."""

from __future__ import annotations

import logging

from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.exceptions import PermissionDenied

from apps.accounts.models import User
from apps.groups.constants import GroupStatus, MemberStatus
from apps.groups.models import Group, GroupMemberRole, GroupPermission, Permission, PermissionState

logger = logging.getLogger(__name__)


def has_perm(user: User | AnonymousUser, group: Group | None, permission_code: str) -> bool:
    if isinstance(user, AnonymousUser):
        return False
    current = User.objects.filter(pk=user.pk).first()
    if (
        current is None
        or not current.is_active
        or current.authorization_version != user.authorization_version
    ):
        return False
    if current.role_mfa_required and not current.two_factor_enabled:
        return False
    permission = Permission.objects.filter(code=permission_code).first()
    # A boolean check can never supply Phase 3's two distinct approving users.
    if permission is None or permission.dual_control:
        return False
    platform_version = (
        PermissionState.objects.filter(pk=1).values_list("version", flat=True).first()
    )
    if platform_version is None:
        return False
    scope = "platform"
    group_version = 0
    if group is not None:
        active = Group.objects.filter(pk=group.pk).first()
        if active is None or (
            permission.is_write and active.status not in (GroupStatus.ACTIVE, GroupStatus.DRAFT)
        ):
            return False
        scope = str(active.public_id)
        group_version = active.permission_version
        if (
            permission.is_write
            and GroupMemberRole.objects.filter(
                group_member__group=group,
                group_member__user=current,
                group_member__status=MemberStatus.ACTIVE,
                group_role__template__code="AUDITOR",
            ).exists()
        ):
            return False
    elif permission_code == "groups.create":
        # Creation precedes membership. This is the authenticated-user baseline.
        return True
    key = f"permissions:v1:{current.public_id}:{scope}:{group_version}:{platform_version}:{current.authorization_version}:{permission_code}"
    try:
        value = cache.get(key)
    except Exception:
        value = None
    if isinstance(value, bool):
        return value
    granted = GroupPermission.objects.filter(permission=permission, granted=True)
    platform = granted.filter(
        group_role__group__isnull=True, group_role__platform_assignments__user=current
    ).exists()
    local = (
        group is not None
        and granted.filter(
            group_role__group=group,
            group_role__member_assignments__group_member__group=group,
            group_role__member_assignments__group_member__user=current,
            group_role__member_assignments__group_member__status=MemberStatus.ACTIVE,
        ).exists()
    )
    result = bool(platform or local)
    try:
        cache.set(key, result, timeout=300)
    except Exception:
        logger.warning("permissions.cache_write_unavailable")
    return result


def require_perm(user: User, group: Group | None, permission_code: str) -> None:
    if not has_perm(user, group, permission_code):
        raise PermissionDenied("You do not have permission to perform this action.")
