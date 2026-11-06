"""Group creation, frozen matrix and database-owned role/session state."""

import pytest
from django.apps import apps
from django.db import IntegrityError, transaction

from apps.groups.catalogue import seed
from apps.groups.models import GroupMemberRole, GroupPermission, GroupRole, GroupType, Permission
from apps.groups.permissions import has_perm
from apps.groups.services import create_group
from tests.factories import UserFactory
from tests.factories.locations import LocationFactory

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.django_db]


def test_group_creation_sets_owner_history_mfa_and_database_version():
    with transaction.atomic():
        seed(apps)
        user = UserFactory()
        group, invitations = create_group(
            actor=user,
            name="Foundation",
            group_type=GroupType.objects.get(code="DAILY_CONTRIBUTION"),
            location=LocationFactory(),
        )
        user.refresh_from_db()
        assert group.owner_id == user.pk and invitations == []
        assert set(group.roles.values_list("code", flat=True)) == {
            "GROUP_OWNER",
            "COLLECTOR",
            "MEMBER",
        }
        member = group.members.get(user=user)
        assert member.history.get().to_status == "ACTIVE"
        assert user.authorization_version > 0 and user.role_mfa_required
        assert not has_perm(user, group, "groups.view")
        user.two_factor_enabled = True
        user.save(update_fields=["two_factor_enabled"])
        assert has_perm(user, group, "groups.view")
        assert has_perm(user, group, "groups.update")
        assert not has_perm(user, group, "settlements.submit")


def test_forbidden_grant_and_self_escalation_are_rejected_by_database():
    seed(apps)
    user = UserFactory()
    group, _ = create_group(
        actor=user,
        name="Guards",
        group_type=GroupType.objects.get(code="DAILY_CONTRIBUTION"),
        location=LocationFactory(),
    )
    user.refresh_from_db()
    collector = group.roles.get(code="COLLECTOR")
    member = group.members.get(user=user)
    with pytest.raises(IntegrityError), transaction.atomic():
        GroupPermission.objects.create(
            group_role=collector,
            permission=Permission.objects.get(code="settlements.verify"),
            granted=True,
            changed_by=user,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        GroupMemberRole.objects.create(group_member=member, group_role=collector, assigned_by=user)


def test_seed_is_idempotent_and_auditor_has_no_write_grants():
    seed(apps)
    seed(apps)
    assert Permission.objects.count() == 71 and GroupType.objects.count() == 6
    assert GroupRole.objects.filter(group=None).count() == 3
    from apps.groups.models import RoleTemplate

    template = RoleTemplate.objects.get(code="AUDITOR")
    assert not Permission.objects.filter(code__in=template.policies, is_write=True).exists()


def test_seed_preserves_grant_history_after_its_editor_loses_platform_authority():
    from apps.groups.services import (
        assign_platform_role,
        bootstrap_platform,
        configure_platform_role,
        revoke_platform_role,
    )

    seed(apps)
    first = UserFactory(is_superuser=True, two_factor_enabled=True)
    second = UserFactory(two_factor_enabled=True)
    bootstrap_platform(user=first)
    first.refresh_from_db()
    role = GroupRole.objects.get(group=None, code="SUPER_ADMIN")
    configure_platform_role(actor=first, role=role, grants={"platform.manage_users": True})
    assign_platform_role(actor=first, target=second, role=role)
    second.refresh_from_db()
    revoke_platform_role(actor=second, target=first, role=role)
    seed(apps)
    grant = GroupPermission.objects.get(group_role=role, permission__code="platform.manage_users")
    assert grant.granted and grant.changed_by_id == first.pk
