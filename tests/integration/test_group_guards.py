"""Direct ORM writes cannot bypass group scope, identity, or safety bounds."""

import pytest
from django.apps import apps
from django.db import IntegrityError, connection, transaction

from apps.groups.catalogue import seed
from apps.groups.models import (
    GroupMemberRole,
    GroupPermission,
    GroupType,
    Permission,
    PlatformRoleAssignment,
    RoleTemplate,
)
from apps.groups.services import bootstrap_platform, create_group
from tests.factories import UserFactory
from tests.factories.locations import LocationFactory

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.django_db]


@pytest.fixture
def foundation():
    seed(apps)
    owner = UserFactory(two_factor_enabled=True)
    group, _ = create_group(
        actor=owner,
        name="Database guards",
        group_type=GroupType.objects.get(code="DAILY_CONTRIBUTION"),
        location=LocationFactory(),
    )
    owner.refresh_from_db()
    return owner, group


def _reject(action):
    with pytest.raises(IntegrityError), transaction.atomic():
        action()
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_owner_cannot_be_demoted_removed_or_replaced_directly(foundation):
    owner, group = foundation
    member = group.members.get(user=owner)
    _reject(lambda: member.role_assignments.filter(group_role__code="GROUP_OWNER").delete())
    _reject(lambda: group.members.filter(pk=member.pk).update(status="SUSPENDED"))
    target = UserFactory()
    _reject(lambda: type(group).objects.filter(pk=group.pk).update(owner=target))
    member.refresh_from_db()
    assert (
        member.status == "ACTIVE"
        and member.role_assignments.filter(group_role__code="GROUP_OWNER").exists()
    )


def test_history_identity_and_catalogue_are_immutable(foundation):
    owner, group = foundation
    member = group.members.get(user=owner)
    target = UserFactory()
    _reject(lambda: member.history.update(reason="rewritten"))
    _reject(lambda: member.history.all().delete())
    _reject(lambda: group.members.filter(pk=member.pk).delete())
    _reject(lambda: type(group).objects.filter(pk=group.pk).delete())
    _reject(lambda: group.members.filter(pk=member.pk).update(user=target))
    _reject(lambda: type(group).objects.filter(pk=group.pk).update(created_by=target))
    _reject(
        lambda: group.roles.filter(code="COLLECTOR").update(
            template=RoleTemplate.objects.get(code="GROUP_OWNER")
        )
    )
    _reject(lambda: RoleTemplate.objects.filter(code="MEMBER").update(policies={}))
    _reject(lambda: Permission.objects.filter(code="groups.update").update(is_write=False))


def test_grant_bounds_cross_scope_and_bootstrap_reuse_are_rejected(foundation):
    owner, group = foundation
    member = group.members.get(user=owner)
    required = GroupPermission.objects.get(
        group_role=group.roles.get(code="GROUP_OWNER"), permission__code="groups.update"
    )
    _reject(
        lambda: GroupPermission.objects.filter(pk=required.pk).update(
            granted=False, changed_by=owner
        )
    )
    _reject(lambda: GroupPermission.objects.filter(pk=required.pk).delete())
    _reject(
        lambda: GroupMemberRole.objects.create(
            group_member=member,
            group_role=group.roles.get(code="COLLECTOR"),
            assigned_by=owner,
            bootstrap=True,
        )
    )
    other, _ = create_group(
        actor=owner, name="Other scope", group_type=group.group_type, location=group.location
    )
    _reject(
        lambda: GroupMemberRole.objects.create(
            group_member=member, group_role=other.roles.get(code="COLLECTOR"), assigned_by=owner
        )
    )
    _reject(
        lambda: PlatformRoleAssignment.objects.create(
            user=owner, role=group.roles.get(code="COLLECTOR"), assigned_by=owner
        )
    )


def test_last_platform_super_admin_is_protected(foundation):
    owner, _ = foundation
    owner.is_superuser = True
    owner.save(update_fields=["is_superuser"])
    assignment = bootstrap_platform(user=owner)
    _reject(lambda: PlatformRoleAssignment.objects.filter(pk=assignment.pk).delete())
    assert PlatformRoleAssignment.objects.filter(pk=assignment.pk).exists()
