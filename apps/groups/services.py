"""Authorised, atomic group lifecycles and audit records."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any

from django.apps import apps
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.constants import ActorType
from apps.audit.services import AuditContext, record
from apps.groups.catalogue import seed
from apps.groups.constants import GroupStatus, MembershipMode, MemberStatus
from apps.groups.errors import GroupConflict, GroupNotFound, GroupRuleViolation
from apps.groups.models import (
    Group,
    GroupInvitation,
    GroupMember,
    GroupMemberRole,
    GroupPermission,
    GroupRole,
    GroupType,
    Permission,
    PermissionState,
    PlatformRoleAssignment,
    RoleSet,
    RoleTemplate,
)
from apps.groups.permissions import has_perm, require_perm
from apps.locations.models import Location
from config.api.exceptions import SeparationOfDuties


def _audit(
    actor: User,
    action: str,
    obj: Any,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    context: AuditContext | None = None,
) -> None:
    record(
        action=action,
        actor_type=ActorType.USER,
        actor_user_id=actor.pk,
        object_type=obj._meta.db_table,
        object_id=str(getattr(obj, "public_id", obj.pk)),
        previous_value=before,
        new_value=after,
        context=context,
    )


def _users(*subjects: User) -> None:
    list(User.objects.select_for_update().filter(pk__in=[u.pk for u in subjects]).order_by("pk"))


def _lock(group: Group) -> Group:
    current = Group.objects.select_for_update().filter(pk=group.pk).first()
    if current is None:
        raise GroupNotFound("No such group.")
    return current


def _member(group: Group, member: GroupMember) -> GroupMember:
    current = GroupMember.objects.select_related("user").filter(pk=member.pk, group=group).first()
    if current is None:
        raise GroupNotFound("No such membership.")
    return current


def _roles(group: Group, templates: list[RoleTemplate]) -> None:
    for template in templates:
        role = GroupRole.objects.create(
            group=group, template=template, code=template.code, name=template.name, is_system=True
        )
        _grants(role)


def _grants(role: GroupRole) -> None:
    for code, policy in role.template.policies.items():
        GroupPermission.objects.create(
            group_role=role,
            permission=Permission.objects.get(code=code),
            granted=policy in ("required", "dual"),
        )


def _baseline(member: GroupMember, actor: User, *, bootstrap: bool = False) -> None:
    GroupMemberRole.objects.get_or_create(
        group_member=member,
        group_role=GroupRole.objects.get(group=member.group, code="MEMBER"),
        defaults={"assigned_by": actor, "bootstrap": bootstrap},
    )


def invitation_digest(token: str) -> str:
    return hashlib.sha256(f"ihweza.group-invitation.v1:{token}".encode()).hexdigest()


def _invitation(group: Group, actor: User, target: User) -> tuple[GroupInvitation, str]:
    stamp = timezone.now()
    GroupInvitation.objects.filter(
        group=group,
        invited_user=target,
        accepted_at__isnull=True,
        declined_at__isnull=True,
        expires_at__gt=stamp,
    ).update(expires_at=stamp)
    token = secrets.token_urlsafe(48)
    ttl = getattr(settings, "GROUP_INVITATION_TTL_SECONDS", 7 * 24 * 3600)
    row = GroupInvitation.objects.create(
        group=group,
        invited_user=target,
        invited_by=actor,
        token_hash=invitation_digest(token),
        expires_at=stamp + timedelta(seconds=ttl),
    )
    return row, token


def create_group(
    *,
    actor: User,
    name: str,
    group_type: GroupType,
    location: Location,
    description: str = "",
    logo: str = "",
    rules: str = "",
    membership_mode: str = MembershipMode.INVITE_ONLY,
    requires_membership_approval: bool = True,
    officers: list[dict[str, Any]] | None = None,
    context: AuditContext | None = None,
) -> tuple[Group, list[dict[str, Any]]]:
    with transaction.atomic():
        _users(actor, *[o["user"] for o in officers or []])
        require_perm(actor, None, "groups.create")
        if (
            not location.is_active
            or not Location.objects.filter(pk=location.pk, is_active=True).exists()
        ):
            raise GroupRuleViolation("Choose an active location.")
        if not name.strip() or membership_mode not in MembershipMode.values:
            raise GroupRuleViolation("Choose a name and a valid membership mode.")
        templates = list(group_type.default_role_set.roles.all())
        names = {t.code for t in templates}
        for structural in ("GROUP_OWNER", "MEMBER"):
            if structural not in names:
                templates.append(RoleTemplate.objects.get(code=structural))
                names.add(structural)
        seen = set()
        for officer in officers or []:
            target = officer["user"]
            if (
                target.pk == actor.pk
                or target.pk in seen
                or not target.is_active
                or not officer["roles"]
                or not set(officer["roles"]).issubset(names)
            ):
                raise GroupRuleViolation(
                    "Initial officers must be distinct active users with roles from the group template."
                )
            seen.add(target.pk)
        group = Group.objects.create(
            name=name.strip(),
            group_type=group_type,
            location=location,
            owner=actor,
            created_by=actor,
            description=description,
            logo=logo,
            rules=rules,
            membership_mode=membership_mode,
            requires_membership_approval=requires_membership_approval,
            status=GroupStatus.DRAFT,
        )
        _roles(group, templates)
        member = GroupMember.objects.create(
            group=group,
            user=actor,
            status=MemberStatus.ACTIVE,
            joined_at=timezone.now(),
            approved_by=actor,
            approved_at=timezone.now(),
            status_changed_by=actor,
        )
        _baseline(member, actor, bootstrap=True)
        GroupMemberRole.objects.create(
            group_member=member,
            group_role=group.roles.get(code="GROUP_OWNER"),
            assigned_by=actor,
            bootstrap=True,
        )
        invitations = []
        for officer in officers or []:
            target = officer["user"]
            member = GroupMember.objects.create(
                group=group, user=target, status=MemberStatus.INVITED, status_changed_by=actor
            )
            for code in set(officer["roles"]) | {"MEMBER"}:
                GroupMemberRole.objects.create(
                    group_member=member,
                    group_role=group.roles.get(code=code),
                    assigned_by=actor,
                    bootstrap=True,
                )
            invitation, token = _invitation(group, actor, target)
            invitations.append(
                {"id": str(invitation.public_id), "user": str(target.public_id), "token": token}
            )
        group.status = GroupStatus.ACTIVE
        group.save(update_fields=["status", "updated_at"])
        _audit(
            actor,
            "group.created",
            group,
            after={
                "group_type": group_type.code,
                "location": str(location.public_id),
                "owner": str(actor.public_id),
                "officers": [
                    {"user": str(o["user"].public_id), "roles": o["roles"]} for o in officers or []
                ],
            },
            context=context,
        )
        group.refresh_from_db()
        return group, invitations


def update_group(
    *, actor: User, group: Group, values: dict[str, Any], context: AuditContext | None = None
) -> Group:
    allowed = {"name", "description", "logo", "group_type", "location"}
    if not set(values).issubset(allowed):
        raise GroupRuleViolation("Use the dedicated settings and rules operations.")
    with transaction.atomic():
        group = _lock(group)
        _users(actor)
        require_perm(actor, group, "groups.update")
        if "location" in values and not values["location"].is_active:
            raise GroupRuleViolation("Choose an active location.")
        before = {key: str(getattr(group, key)) for key in values}
        for key, value in values.items():
            setattr(group, key, value)
        group.save(update_fields=[*values, "updated_at"])
        _audit(
            actor,
            "group.updated",
            group,
            before=before,
            after={k: str(v) for k, v in values.items()},
            context=context,
        )
        group.refresh_from_db()
        return group


def configure(
    *,
    actor: User,
    group: Group,
    values: dict[str, Any],
    rules: bool = False,
    context: AuditContext | None = None,
) -> Group:
    allowed = {"rules"} if rules else {"membership_mode", "requires_membership_approval"}
    if not set(values).issubset(allowed) or (
        "membership_mode" in values and values["membership_mode"] not in MembershipMode.values
    ):
        raise GroupRuleViolation("Invalid group configuration.")
    with transaction.atomic():
        group = _lock(group)
        _users(actor)
        require_perm(actor, group, "groups.manage_rules" if rules else "groups.manage_settings")
        before = {k: getattr(group, k) for k in values}
        for key, value in values.items():
            setattr(group, key, value)
        group.save(update_fields=[*values, "updated_at"])
        _audit(
            actor,
            "group.rules_updated" if rules else "group.settings_updated",
            group,
            before=before,
            after=values,
            context=context,
        )
        return group


def archive(*, actor: User, group: Group, context: AuditContext | None = None) -> Group:
    with transaction.atomic():
        group = _lock(group)
        _users(actor)
        require_perm(actor, group, "groups.archive")
        if group.status not in (GroupStatus.ACTIVE, GroupStatus.DRAFT):
            raise GroupConflict("The group is already inactive.")
        before = group.status
        group.status = GroupStatus.ARCHIVED
        group.save(update_fields=["status", "updated_at"])
        _audit(
            actor,
            "group.archived",
            group,
            before={"status": before},
            after={"status": group.status},
            context=context,
        )
        return group


def join(*, actor: User, group: Group, context: AuditContext | None = None) -> GroupMember:
    with transaction.atomic():
        group = _lock(group)
        _users(actor)
        current = User.objects.get(pk=actor.pk)
        if not current.is_active or current.authorization_version != actor.authorization_version:
            raise PermissionDenied()
        if (
            group.status != GroupStatus.ACTIVE
            or group.membership_mode == MembershipMode.INVITE_ONLY
        ):
            raise GroupNotFound("This group is not accepting applications.")
        member, created = GroupMember.objects.get_or_create(
            group=group, user=actor, defaults={"status_changed_by": actor}
        )
        if not created and member.status not in (MemberStatus.LEFT, MemberStatus.REMOVED):
            raise GroupConflict("A membership already exists.")
        member.status = (
            MemberStatus.PENDING_APPROVAL
            if group.requires_membership_approval
            else MemberStatus.ACTIVE
        )
        member.joined_at = timezone.now()
        member.left_at = None
        member.suspended_at = None
        member.suspension_reason = ""
        member.status_changed_by = actor
        member.save()
        if member.status == MemberStatus.ACTIVE:
            _baseline(member, actor)
        _audit(actor, "member.joined", member, after={"status": member.status}, context=context)
        return member


def invite(
    *, actor: User, group: Group, target: User, context: AuditContext | None = None
) -> tuple[GroupInvitation, str]:
    with transaction.atomic():
        group = _lock(group)
        _users(actor, target)
        require_perm(actor, group, "members.invite")
        if not User.objects.filter(pk=target.pk, account_status="ACTIVE").exists():
            raise GroupRuleViolation("Choose an active user.")
        member, created = GroupMember.objects.get_or_create(
            group=group,
            user=target,
            defaults={"status": MemberStatus.INVITED, "status_changed_by": actor},
        )
        if not created and member.status not in (
            MemberStatus.INVITED,
            MemberStatus.LEFT,
            MemberStatus.REMOVED,
        ):
            raise GroupConflict("This user already has a membership.")
        if member.status != MemberStatus.INVITED:
            member.status = MemberStatus.INVITED
            member.status_changed_by = actor
            member.save(update_fields=["status", "status_changed_by", "updated_at"])
        row, token = _invitation(group, actor, target)
        _audit(
            actor,
            "member.invited",
            member,
            after={"invitation": str(row.public_id)},
            context=context,
        )
        return row, token


def respond_invitation(
    *, actor: User, token: str, accept: bool, context: AuditContext | None = None
) -> GroupMember | None:
    candidate = GroupInvitation.objects.filter(token_hash=invitation_digest(token)).first()
    if candidate is None:
        raise GroupNotFound("No such invitation.")
    with transaction.atomic():
        group = _lock(candidate.group)
        _users(actor)
        row = GroupInvitation.objects.select_for_update().get(pk=candidate.pk)
        current = User.objects.get(pk=actor.pk)
        if (
            not current.is_active
            or current.authorization_version != actor.authorization_version
            or row.invited_user_id != actor.pk
            or row.accepted_at
            or row.declined_at
            or row.expires_at <= timezone.now()
            or group.status != GroupStatus.ACTIVE
        ):
            raise GroupNotFound("No such invitation.")
        member = GroupMember.objects.get(group=group, user=actor)
        if member.status != MemberStatus.INVITED:
            raise GroupConflict("This invitation no longer applies.")
        if accept:
            row.accepted_at = timezone.now()
            member.status = (
                MemberStatus.PENDING_APPROVAL
                if group.requires_membership_approval
                else MemberStatus.ACTIVE
            )
            member.joined_at = timezone.now()
            member.status_changed_by = actor
            member.save(update_fields=["status", "joined_at", "status_changed_by", "updated_at"])
            if member.status == MemberStatus.ACTIVE:
                _baseline(member, actor)
        else:
            row.declined_at = timezone.now()
            member.status = MemberStatus.LEFT
            member.left_at = timezone.now()
            member.status_changed_by = actor
            member.save(update_fields=["status", "left_at", "status_changed_by", "updated_at"])
            member.role_assignments.all().delete()
        row.save(update_fields=["accepted_at", "declined_at"])
        _audit(
            actor, "invitation.accepted" if accept else "invitation.declined", row, context=context
        )
        return member if accept else None


def transition_member(
    *,
    actor: User,
    group: Group,
    member: GroupMember,
    action: str,
    reason: str = "",
    context: AuditContext | None = None,
) -> GroupMember:
    transitions = {
        "approve": (MemberStatus.PENDING_APPROVAL, MemberStatus.ACTIVE, "members.approve"),
        "suspend": (MemberStatus.ACTIVE, MemberStatus.SUSPENDED, "members.suspend"),
        "reinstate": (MemberStatus.SUSPENDED, MemberStatus.ACTIVE, "members.suspend"),
        "remove": (None, MemberStatus.REMOVED, "members.remove"),
        "leave": (None, MemberStatus.LEFT, None),
    }
    if action not in transitions:
        raise GroupRuleViolation("Unknown membership action.")
    expected, destination, permission = transitions[action]
    with transaction.atomic():
        group = _lock(group)
        member = _member(group, member)
        _users(actor, member.user)
        if group.status != GroupStatus.ACTIVE:
            raise GroupConflict("Inactive groups cannot change membership.")
        if permission:
            require_perm(actor, group, permission)
        elif (
            actor.pk != member.user_id
            or not User.objects.filter(
                pk=actor.pk,
                account_status="ACTIVE",
                authorization_version=actor.authorization_version,
            ).exists()
        ):
            raise PermissionDenied()
        if member.user_id == group.owner_id and destination != MemberStatus.ACTIVE:
            raise GroupRuleViolation("Transfer ownership before suspending or removing the owner.")
        if (
            (expected is not None and member.status != expected)
            or member.status in (MemberStatus.LEFT, MemberStatus.REMOVED)
            or (
                action == "leave"
                and member.status not in (MemberStatus.ACTIVE, MemberStatus.SUSPENDED)
            )
        ):
            raise GroupConflict("Invalid membership transition.")
        before = member.status
        member.status = destination
        member.status_changed_by = actor
        member.suspension_reason = reason
        if action == "approve":
            member.approved_by = actor
            member.approved_at = timezone.now()
        if action == "suspend":
            member.suspended_at = timezone.now()
        if action in ("leave", "remove"):
            member.left_at = timezone.now()
        member.save()
        if destination == MemberStatus.ACTIVE:
            _baseline(member, actor)
        if destination in (MemberStatus.LEFT, MemberStatus.REMOVED):
            member.role_assignments.all().delete()
        _audit(
            actor,
            f"member.{ {'approve': 'approved', 'suspend': 'suspended', 'reinstate': 'reinstated', 'remove': 'removed', 'leave': 'left'}[action] }",
            member,
            before={"status": before},
            after={"status": destination},
            context=context,
        )
        return member


def create_role(
    *,
    actor: User,
    group: Group,
    code: str,
    name: str,
    template: RoleTemplate,
    description: str = "",
    context: AuditContext | None = None,
) -> GroupRole:
    with transaction.atomic():
        group = _lock(group)
        _users(actor)
        require_perm(actor, group, "groups.manage_settings")
        if (
            template.scope != "GROUP"
            or not code
            or code in {t.code for t in RoleTemplate.objects.all()}
        ):
            raise GroupRuleViolation("Choose a new role code and a group template.")
        if GroupRole.objects.filter(group=group, code=code).exists():
            raise GroupConflict("This role code already exists.")
        role = GroupRole.objects.create(
            group=group, template=template, code=code, name=name, description=description
        )
        _grants(role)
        _audit(
            actor,
            "role.created",
            role,
            after={"code": code, "template": template.code},
            context=context,
        )
        return role


def configure_role(
    *,
    actor: User,
    group: Group,
    role: GroupRole,
    grants: dict[str, bool],
    context: AuditContext | None = None,
) -> GroupRole:
    with transaction.atomic():
        group = _lock(group)
        _users(actor)
        require_perm(actor, group, "groups.manage_settings")
        candidate = (
            GroupRole.objects.select_related("template")
            .filter(pk=role.pk, group=group, is_configurable=True)
            .first()
        )
        if candidate is None:
            raise GroupNotFound("No configurable role in this group.")
        role = candidate
        known = set(Permission.objects.values_list("code", flat=True))
        if not set(grants).issubset(known):
            raise GroupRuleViolation("Unknown permission code.")
        held = role.member_assignments.filter(
            group_member__user=actor, group_member__status=MemberStatus.ACTIVE
        ).exists()
        before = {
            g.permission.code: g.granted
            for g in role.permission_grants.select_related("permission")
        }
        for code, value in grants.items():
            policy = role.template.policies.get(code)
            if (value and policy is None) or (not value and policy in ("required", "dual")):
                raise GroupRuleViolation("This permission is outside the role's safety bounds.")
            if value and not before.get(code, False) and held:
                raise SeparationOfDuties("You cannot grant yourself a permission.")
        for code, value in grants.items():
            GroupPermission.objects.update_or_create(
                group_role=role,
                permission=Permission.objects.get(code=code),
                defaults={"granted": value, "changed_by": actor},
            )
        _audit(
            actor, "role.permissions_updated", role, before=before, after=grants, context=context
        )
        return role


def assign_role(
    *,
    actor: User,
    group: Group,
    member: GroupMember,
    role: GroupRole,
    context: AuditContext | None = None,
) -> GroupMemberRole:
    with transaction.atomic():
        group = _lock(group)
        member = _member(group, member)
        _users(actor, member.user)
        require_perm(actor, group, "members.assign_roles")
        if member.user_id == actor.pk:
            raise SeparationOfDuties("You cannot assign yourself a role.")
        if role.group_id != group.pk:
            raise GroupNotFound("No such role in this group.")
        if member.status != MemberStatus.ACTIVE:
            raise GroupConflict("Roles require an active membership.")
        held = GroupMemberRole.objects.filter(
            group_member__group=group,
            group_member__user=actor,
            group_member__status=MemberStatus.ACTIVE,
        )
        if not held.filter(group_role=role).exists():
            if role.is_system or not held.filter(group_role__template=role.template).exists():
                raise SeparationOfDuties("You cannot grant a role you do not hold.")
            for grant in role.permission_grants.filter(granted=True).select_related("permission"):
                if (
                    not has_perm(actor, group, grant.permission.code)
                    and not grant.permission.dual_control
                ):
                    raise SeparationOfDuties("The custom role exceeds your authority.")
        assignment, created = GroupMemberRole.objects.get_or_create(
            group_member=member, group_role=role, defaults={"assigned_by": actor}
        )
        if created:
            _audit(
                actor,
                "member.role_assigned",
                member,
                after={"role": str(role.public_id)},
                context=context,
            )
        return assignment


def revoke_role(
    *,
    actor: User,
    group: Group,
    member: GroupMember,
    role: GroupRole,
    context: AuditContext | None = None,
) -> None:
    with transaction.atomic():
        group = _lock(group)
        member = _member(group, member)
        _users(actor, member.user)
        require_perm(actor, group, "members.assign_roles")
        if member.user_id == actor.pk:
            raise SeparationOfDuties(
                "Use ownership transfer or ask another authorised officer to change your roles."
            )
        if role.group_id != group.pk:
            raise GroupNotFound("No such role in this group.")
        if member.user_id == group.owner_id and role.code == "GROUP_OWNER":
            raise GroupRuleViolation("Transfer ownership first.")
        removed, _ = GroupMemberRole.objects.filter(group_member=member, group_role=role).delete()
        if removed:
            _audit(
                actor,
                "member.role_revoked",
                member,
                before={"role": str(role.public_id)},
                context=context,
            )


def transfer_ownership(
    *, actor: User, group: Group, member: GroupMember, context: AuditContext | None = None
) -> Group:
    with transaction.atomic():
        group = _lock(group)
        member = _member(group, member)
        _users(actor, member.user)
        require_perm(actor, group, "groups.transfer_ownership")
        if (
            group.owner_id != actor.pk
            or member.user_id == actor.pk
            or member.status != MemberStatus.ACTIVE
        ):
            raise GroupRuleViolation("The current owner must choose another active member.")
        role = group.roles.get(code="GROUP_OWNER")
        GroupMemberRole.objects.get_or_create(
            group_member=member, group_role=role, defaults={"assigned_by": actor}
        )
        previous = group.owner
        group.owner = member.user
        group.save(update_fields=["owner", "updated_at"])
        GroupMemberRole.objects.filter(
            group_member__group=group, group_member__user=previous, group_role=role
        ).delete()
        _audit(
            actor,
            "group.ownership_transferred",
            group,
            before={"owner": str(previous.public_id)},
            after={"owner": str(member.user.public_id)},
            context=context,
        )
        return group


def bootstrap_platform(*, user: User) -> PlatformRoleAssignment:
    with transaction.atomic():
        seed(apps)
        PermissionState.objects.select_for_update().get(pk=1)
        _users(user)
        if PlatformRoleAssignment.objects.filter(role__code="SUPER_ADMIN").exists():
            raise GroupConflict("A platform super admin already exists.")
        user = User.objects.get(pk=user.pk)
        if not user.is_active or not user.is_superuser or not user.two_factor_enabled:
            raise GroupRuleViolation(
                "Bootstrap requires an active Django superuser with confirmed MFA."
            )
        role = GroupRole.objects.get(group=None, code="SUPER_ADMIN")
        assignment = PlatformRoleAssignment.objects.create(user=user, role=role)
        record(
            action="platform.super_admin_bootstrapped",
            actor_type=ActorType.SYSTEM,
            object_type="platform_role_assignments",
            object_id=str(assignment.pk),
            new_value={"user": str(user.public_id), "role": role.code},
        )
        return assignment


def assign_platform_role(
    *, actor: User, target: User, role: GroupRole, context: AuditContext | None = None
) -> PlatformRoleAssignment:
    with transaction.atomic():
        PermissionState.objects.select_for_update().get(pk=1)
        _users(actor, target)
        require_perm(actor, None, "platform.manage_users")
        if (
            target.pk == actor.pk
            or role.group_id is not None
            or not PlatformRoleAssignment.objects.filter(
                user=actor, role__code="SUPER_ADMIN", role__group__isnull=True
            ).exists()
        ):
            raise SeparationOfDuties("Only a super admin can assign another user's platform roles.")
        assignment, created = PlatformRoleAssignment.objects.get_or_create(
            user=target, role=role, defaults={"assigned_by": actor}
        )
        if created:
            _audit(
                actor, "platform.role_assigned", target, after={"role": role.code}, context=context
            )
        return assignment


def rename_role(
    *,
    actor: User,
    group: Group,
    role: GroupRole,
    values: dict[str, str],
    context: AuditContext | None = None,
) -> GroupRole:
    with transaction.atomic():
        group = _lock(group)
        _users(actor)
        require_perm(actor, group, "groups.manage_settings")
        candidate = GroupRole.objects.filter(pk=role.pk, group=group).first()
        if candidate is None:
            raise GroupNotFound("No such role.")
        role = candidate
        if role.is_system or not set(values).issubset({"name", "description"}):
            raise GroupRuleViolation("Only non-system role names and descriptions can be changed.")
        before = {k: getattr(role, k) for k in values}
        for key, value in values.items():
            setattr(role, key, value)
        role.save(update_fields=list(values))
        _audit(actor, "role.updated", role, before=before, after=values, context=context)
        return role


def delete_role(
    *, actor: User, group: Group, role: GroupRole, context: AuditContext | None = None
) -> None:
    with transaction.atomic():
        group = _lock(group)
        _users(actor)
        require_perm(actor, group, "groups.manage_settings")
        current = GroupRole.objects.filter(pk=role.pk, group=group).first()
        if current is None:
            raise GroupNotFound("No such role.")
        if current.is_system or current.member_assignments.exists():
            raise GroupRuleViolation("System roles and roles with assignments cannot be deleted.")
        # Delete the role first under deferred FKs. The grant guard then knows
        # this is role removal, rather than deletion of a required live grant.
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM group_roles WHERE id = %s", [current.pk])
        GroupPermission.objects.filter(group_role_id=current.pk).delete()
        _audit(actor, "role.deleted", current, context=context)


def revoke_platform_role(
    *, actor: User, target: User, role: GroupRole, context: AuditContext | None = None
) -> None:
    with transaction.atomic():
        PermissionState.objects.select_for_update().get(pk=1)
        _users(actor, target)
        require_perm(actor, None, "platform.manage_users")
        if (
            actor.pk == target.pk
            or role.group_id is not None
            or not PlatformRoleAssignment.objects.filter(
                user=actor, role__code="SUPER_ADMIN", role__group__isnull=True
            ).exists()
        ):
            raise SeparationOfDuties("Only a super admin can revoke another user's platform roles.")
        if (
            role.code == "SUPER_ADMIN"
            and PlatformRoleAssignment.objects.filter(role=role).count() <= 1
        ):
            raise GroupRuleViolation("The last super admin is protected.")
        removed, _ = PlatformRoleAssignment.objects.filter(user=target, role=role).delete()
        if removed:
            _audit(
                actor, "platform.role_revoked", target, before={"role": role.code}, context=context
            )


def configure_platform_role(
    *, actor: User, role: GroupRole, grants: dict[str, bool], context: AuditContext | None = None
) -> GroupRole:
    with transaction.atomic():
        PermissionState.objects.select_for_update().get(pk=1)
        _users(actor)
        require_perm(actor, None, "settings.manage_platform")
        if role.group_id is not None:
            raise GroupNotFound("No such platform role.")
        before = {
            g.permission.code: g.granted
            for g in role.permission_grants.select_related("permission")
        }
        own = PlatformRoleAssignment.objects.filter(user=actor, role=role).exists()
        for code, value in grants.items():
            policy = role.template.policies.get(code)
            if (
                not Permission.objects.filter(code=code).exists()
                or (value and policy is None)
                or (not value and policy in ("required", "dual"))
            ):
                raise GroupRuleViolation("This permission is outside platform safety bounds.")
            if own and value and not before.get(code, False):
                raise SeparationOfDuties("You cannot grant yourself a permission.")
        for code, value in grants.items():
            GroupPermission.objects.update_or_create(
                group_role=role,
                permission=Permission.objects.get(code=code),
                defaults={"granted": value, "changed_by": actor},
            )
        _audit(
            actor,
            "platform.role_permissions_updated",
            role,
            before=before,
            after=grants,
            context=context,
        )
        return role


def create_group_type(
    *,
    actor: User,
    code: str,
    name: str,
    roles: list[RoleTemplate],
    description: str = "",
    context: AuditContext | None = None,
) -> GroupType:
    with transaction.atomic():
        PermissionState.objects.select_for_update().get(pk=1)
        _users(actor)
        require_perm(actor, None, "platform.manage_group_types")
        if GroupType.objects.filter(code=code).exists():
            raise GroupConflict("This group type already exists.")
        if not roles or any(r.scope != "GROUP" for r in roles):
            raise GroupRuleViolation("Choose group-scoped templates.")
        role_set = RoleSet.objects.create(code=code)
        role_set.roles.set(roles)
        result = GroupType.objects.create(
            code=code, name=name, description=description, default_role_set=role_set
        )
        _audit(
            actor,
            "group_type.created",
            result,
            after={"code": code, "roles": [r.code for r in roles]},
            context=context,
        )
        return result


def update_group_type(
    *,
    actor: User,
    group_type: GroupType,
    values: dict[str, Any],
    context: AuditContext | None = None,
) -> GroupType:
    if not set(values).issubset({"name", "description", "roles"}):
        raise GroupRuleViolation("Group type identity cannot be changed.")
    with transaction.atomic():
        PermissionState.objects.select_for_update().get(pk=1)
        _users(actor)
        require_perm(actor, None, "platform.manage_group_types")
        current = GroupType.objects.select_for_update().get(pk=group_type.pk)
        before = {
            "name": current.name,
            "description": current.description,
            "roles": list(current.default_role_set.roles.values_list("code", flat=True)),
        }
        if "roles" in values:
            roles = values["roles"]
            if not roles or any(r.scope != "GROUP" for r in roles):
                raise GroupRuleViolation("Choose group-scoped templates.")
            # A separate set preserves the platform's override during reseeding.
            role_set = RoleSet.objects.create(code=f"CUSTOM_{secrets.token_hex(12).upper()}")
            role_set.roles.set(roles)
            current.default_role_set = role_set
        for key in ("name", "description"):
            if key in values:
                setattr(current, key, values[key])
        current.save()
        _audit(
            actor,
            "group_type.updated",
            current,
            before=before,
            after={
                "name": current.name,
                "description": current.description,
                "roles": list(current.default_role_set.roles.values_list("code", flat=True)),
            },
            context=context,
        )
        return current


def manage_location(
    *,
    actor: User,
    values: dict[str, Any],
    location: Location | None = None,
    context: AuditContext | None = None,
) -> Location:
    allowed = {"name", "slug", "code", "latitude", "longitude", "is_active"}
    if location is None:
        allowed |= {"country", "location_type", "parent"}
    if not set(values).issubset(allowed):
        raise GroupRuleViolation("Location tree identity cannot be changed.")
    with transaction.atomic():
        PermissionState.objects.select_for_update().get(pk=1)
        _users(actor)
        require_perm(actor, None, "platform.manage_locations")
        current = (
            Location.objects.select_for_update().get(pk=location.pk) if location else Location()
        )
        before = {key: str(getattr(current, key)) for key in values} if location else None
        for key, value in values.items():
            setattr(current, key, value)
        try:
            current.full_clean()
        except ValidationError as exc:
            raise GroupRuleViolation("; ".join(exc.messages)) from exc
        current.save()
        _audit(
            actor,
            "location.updated" if location else "location.created",
            current,
            before=before,
            after={key: str(getattr(current, key)) for key in values},
            context=context,
        )
        return current
